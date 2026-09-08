/**
 * The native surface, as an interface.
 *
 * Four things cannot be obtained from JavaScript and must come from platform
 * code. They are declared here so the TypeScript layer can be written, read and
 * tested against a contract rather than against a mock of a mock — and so an
 * app integrating this SDK knows exactly what its native module must provide.
 *
 * Every one of them is optional, and the SDK degrades rather than fails:
 *
 * - **Install referrer** (Android). The Play Install Referrer API. Without it
 *   Android attribution falls back to device matching, which is weaker but not
 *   nothing.
 * - **Advertising ID** (GAID / IDFA). Requires the user's permission on both
 *   platforms — ATT on iOS, and honouring `isLimitAdTrackingEnabled` on
 *   Android. The SDK asks for it only when the `advertising` consent purpose is
 *   granted, and it is sent raw over TLS because the server hashes it at the
 *   edge under a pepper the device must never hold.
 * - **Device metadata** — OS version, model, app version. Reporting only.
 * - **App-open URL** — the deep link the app was launched with, when it was
 *   launched by one.
 *
 * A missing native module is a normal state, not an error: the SDK is useful
 * on its own and this keeps a JavaScript-only integration working while the
 * native side is still being wired up.
 */
export interface NativeBridge {
  /** The raw Play Install Referrer string, once, on first launch. */
  getInstallReferrer?(): Promise<string | null>;
  /** GAID or IDFA. Must return null when the user has opted out. */
  getAdvertisingId?(): Promise<string | null>;
  getDeviceInfo?(): Promise<DeviceInfo>;
}

export interface DeviceInfo {
  platform?: "android" | "ios";
  osVersion?: string;
  appVersion?: string;
  deviceModel?: string;
}

/** Used when no native module is present. Every call is a no-op returning the
 *  "not available" answer, so no caller needs to branch on its absence. */
export const NO_NATIVE: NativeBridge = {
  async getInstallReferrer() {
    return null;
  },
  async getAdvertisingId() {
    return null;
  },
  async getDeviceInfo() {
    return {};
  },
};

/**
 * Calls a native method that may be absent, may reject, or may hang.
 *
 * All three happen: a module removed by a Proguard rule, a permission dialogue
 * the user dismissed, a Play Services call that never returns on a device with
 * no Play Services. None of them should stop the SDK initialising, so each is
 * bounded and each failure produces the same "not available" answer.
 */
export async function callNative<T>(
  call: (() => Promise<T>) | undefined,
  fallback: T,
  timeoutMs = 3_000,
): Promise<T> {
  if (typeof call !== "function") return fallback;
  try {
    return await Promise.race([
      call(),
      new Promise<T>((resolve) => setTimeout(() => resolve(fallback), timeoutMs)),
    ]);
  } catch {
    return fallback;
  }
}
