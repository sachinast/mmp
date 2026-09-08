package com.mmp

import com.facebook.react.bridge.Promise
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.bridge.ReactContextBaseJavaModule
import com.facebook.react.bridge.ReactMethod
import com.facebook.react.bridge.Arguments
import java.util.concurrent.Executors

/**
 * The React Native bridge. Forwarding only — every decision lives in
 * [MmpIdentifiers], which compiles without React.
 *
 * Written against the classic bridge API, which runs unchanged under the New
 * Architecture through the interop layer.
 */
class MmpNativeModule(context: ReactApplicationContext) :
    ReactContextBaseJavaModule(context) {

    /** One thread, not a pool. These calls happen a handful of times per launch
     *  and a pool would be a thread-per-call for work that is almost always
     *  idle. Daemon, so it can never hold the process open. */
    private val worker = Executors.newSingleThreadExecutor { runnable ->
        Thread(runnable, "mmp-native").apply { isDaemon = true }
    }

    override fun getName(): String = "MmpNative"

    @ReactMethod
    fun getAdvertisingId(promise: Promise) {
        // Off the main thread, without exception: Play Services throws if this
        // is called on it, and the throw would surface as a crash in the host
        // app rather than as a missing identifier.
        worker.execute {
            // Resolved with null rather than rejected. "The user said no" is an
            // answer, not a failure, and rejecting would log an error for a
            // completely normal state.
            promise.resolve(MmpIdentifiers.advertisingId(reactApplicationContext))
        }
    }

    @ReactMethod
    fun getInstallReferrer(promise: Promise) {
        worker.execute {
            MmpIdentifiers.installReferrer(reactApplicationContext) { referrer ->
                promise.resolve(referrer)
            }
        }
    }

    @ReactMethod
    fun getDeviceInfo(promise: Promise) {
        worker.execute {
            val map = Arguments.createMap()
            MmpIdentifiers.deviceInfo(reactApplicationContext).forEach { (key, value) ->
                map.putString(key, value)
            }
            promise.resolve(map)
        }
    }

    override fun invalidate() {
        worker.shutdownNow()
        super.invalidate()
    }
}
