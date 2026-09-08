package com.mmp

import android.content.Context
import android.os.Build
import com.android.installreferrer.api.InstallReferrerClient
import com.android.installreferrer.api.InstallReferrerStateListener
import com.google.android.gms.ads.identifier.AdvertisingIdClient
import java.util.concurrent.atomic.AtomicBoolean

/**
 * The identifier logic, with no React Native dependency.
 *
 * Split from the bridge so the part with decisions in it can be read and
 * reasoned about on its own; the module beside it is a forwarding shim.
 *
 * Two Android-specific traps are handled here, and both are silent when they
 * are not:
 *
 * 1. [advertisingId] blocks. Google Play Services throws if it is called on the
 *    main thread, so the caller must be off it — the bridge uses a single
 *    background executor.
 * 2. An opted-out device does not fail. It returns the all-zero UUID, or sets
 *    `isLimitAdTrackingEnabled`. Treating either as an identifier would give
 *    every opted-out device on earth the same value and make them all match
 *    each other, which is the exact opposite of what the opt-out asked for.
 */
object MmpIdentifiers {

    /** Not an identifier. See the note on this object. */
    private const val ZERO_UUID = "00000000-0000-0000-0000-000000000000"

    /** The referrer service can take a moment on a cold device and can also
     *  never answer at all. Bounded so a first launch is not held open. */
    private const val REFERRER_TIMEOUT_MS = 5_000L

    /**
     * The advertising ID, or null.
     *
     * **Must not be called on the main thread.**
     *
     * Note for the host app: on Android 13 and above this returns zeros unless
     * the app declares `com.google.android.gms.permission.AD_ID` in its
     * manifest. The SDK's own manifest declares it, but a `tools:node="remove"`
     * anywhere in the app will strip it and the failure looks identical to a
     * user opt-out.
     */
    @JvmStatic
    fun advertisingId(context: Context): String? {
        return try {
            val info = AdvertisingIdClient.getAdvertisingIdInfo(context)
            if (info.isLimitAdTrackingEnabled) return null
            val id = info.id
            if (id.isNullOrBlank() || id.equals(ZERO_UUID, ignoreCase = true)) null else id
        } catch (error: Exception) {
            // Every failure means the same thing to the caller: no identifier.
            // Play Services can be absent, out of date, or throw IOException on
            // a device with no Google apps at all, and none of those is worth
            // distinguishing — or worth crashing the host app over.
            null
        }
    }

    /**
     * The Play Install Referrer, read once.
     *
     * This is the highest-fidelity attribution signal on any platform: it comes
     * from Google, survives the install, and cannot be claimed by a competing
     * network. It is also available exactly once, so the result is delivered
     * through [onResult] and the connection is always closed.
     */
    @JvmStatic
    fun installReferrer(context: Context, onResult: (String?) -> Unit) {
        val client = InstallReferrerClient.newBuilder(context).build()

        // The listener is documented as being called once, but a disconnect
        // racing a response has been observed to call it twice. Resolving twice
        // would settle the JavaScript promise twice — harmless — and then close
        // an already-closed connection, which throws. The guard is cheaper than
        // finding out which.
        val settled = AtomicBoolean(false)
        fun finish(referrer: String?) {
            if (!settled.compareAndSet(false, true)) return
            try {
                client.endConnection()
            } catch (_: Exception) {
                // Already closed, or the service went away. Nothing to do.
            }
            onResult(referrer)
        }

        // A device where the service never answers must not hold a first launch
        // open forever, so the timeout resolves it as "no referrer".
        android.os.Handler(android.os.Looper.getMainLooper())
            .postDelayed({ finish(null) }, REFERRER_TIMEOUT_MS)

        try {
            client.startConnection(object : InstallReferrerStateListener {
                override fun onInstallReferrerSetupFinished(responseCode: Int) {
                    if (responseCode != InstallReferrerClient.InstallReferrerResponse.OK) {
                        // FEATURE_NOT_SUPPORTED on a device without the Play
                        // Store, SERVICE_UNAVAILABLE while it updates. Both are
                        // ordinary; attribution falls back to device matching.
                        finish(null)
                        return
                    }
                    finish(
                        try {
                            client.installReferrer.installReferrer
                        } catch (_: Exception) {
                            null
                        },
                    )
                }

                override fun onInstallReferrerServiceDisconnected() {
                    finish(null)
                }
            })
        } catch (_: Exception) {
            finish(null)
        }
    }

    @JvmStatic
    fun deviceInfo(context: Context): Map<String, String> {
        val info = mutableMapOf(
            "platform" to "android",
            "osVersion" to Build.VERSION.RELEASE,
            "deviceModel" to Build.MODEL,
        )
        try {
            val packageInfo = context.packageManager.getPackageInfo(context.packageName, 0)
            packageInfo.versionName?.let { info["appVersion"] = it }
        } catch (_: Exception) {
            // A package that cannot find itself is not a reason to fail; the
            // column is simply empty.
        }
        return info
    }
}
