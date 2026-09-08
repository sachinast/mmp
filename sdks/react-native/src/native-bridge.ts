/**
 * The JavaScript side of the native bridge.
 *
 * Everything here is defensive, because native modules fail in ways JavaScript
 * ones do not: the module can be stripped by a Proguard rule, the method can be
 * missing because the app is on an older build of the native package, the
 * promise can reject from a background thread, and on Android it can simply
 * never settle when Play Services is absent.
 *
 * The most important thing this layer does is treat an *opted-out* advertising
 * ID as no advertising ID. Both platforms answer an opt-out by returning the
 * all-zero UUID rather than by failing, so a naive integration reads
 * "00000000-0000-0000-0000-000000000000", decides it has an identifier, and
 * sends the same value for every opted-out device on earth. The server would
 * then match them all to each other. Checking for it here means the mistake
 * cannot be made by an app that wires the bridge up itself.
 */
import type { DeviceInfo, NativeBridge } from "./native";

/** What a platform returns when the user has opted out. Not an identifier. */
const ZERO_UUID = "00000000-0000-0000-0000-000000000000";

export interface NativeModuleShape {
  getInstallReferrer?(): Promise<string | null>;
  getAdvertisingId?(): Promise<string | null>;
  getDeviceInfo?(): Promise<Record<string, unknown>>;
  updateConversionValue?(fineValue: number, coarseValue: string | null): Promise<boolean>;
}

/**
 * True when the value is a usable advertising identifier.
 *
 * Exported because it is the one piece of this file worth asserting directly:
 * getting it wrong is silent, and its consequence — every opted-out device
 * sharing one identifier — is exactly the outcome the opt-out exists to prevent.
 */
export function isUsableAdvertisingId(value: unknown): value is string {
  if (typeof value !== "string") return false;
  const trimmed = value.trim();
  if (trimmed === "" || trimmed.toLowerCase() === ZERO_UUID) return false;
  // Some devices return "null" or "unknown" as a literal string rather than a
  // null value, which then reads as a perfectly good identifier.
  if (["null", "undefined", "unknown", "none"].includes(trimmed.toLowerCase())) return false;
  return true;
}

function cleanString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() !== "" ? value : undefined;
}

/**
 * Wraps a native module — usually `NativeModules.MmpNative` — as a
 * `NativeBridge`.
 *
 * Passing `undefined` is a supported state, not an error: it is what a
 * JavaScript-only integration looks like, and what an app looks like before the
 * native side has been linked. The result is a bridge that answers "not
 * available" to everything.
 */
export function createNativeBridge(module: NativeModuleShape | undefined): NativeBridge {
  if (!module) return {};

  const bridge: NativeBridge = {};

  if (typeof module.getInstallReferrer === "function") {
    bridge.getInstallReferrer = async () => {
      const referrer = await module.getInstallReferrer!();
      return cleanString(referrer) ?? null;
    };
  }

  if (typeof module.getAdvertisingId === "function") {
    bridge.getAdvertisingId = async () => {
      const id = await module.getAdvertisingId!();
      return isUsableAdvertisingId(id) ? id : null;
    };
  }

  if (typeof module.getDeviceInfo === "function") {
    bridge.getDeviceInfo = async () => {
      const info = (await module.getDeviceInfo!()) ?? {};
      const platform = info["platform"];
      const result: DeviceInfo = {};
      // Rebuilt field by field rather than passed through. A native module is
      // free to return anything, and spreading it would put whatever it invented
      // into the event payload — where the server rejects unknown fields and the
      // whole batch fails.
      if (platform === "android" || platform === "ios") result.platform = platform;
      const osVersion = cleanString(info["osVersion"]);
      if (osVersion) result.osVersion = osVersion;
      const appVersion = cleanString(info["appVersion"]);
      if (appVersion) result.appVersion = appVersion;
      const deviceModel = cleanString(info["deviceModel"]);
      if (deviceModel) result.deviceModel = deviceModel;
      return result;
    };
  }

  if (typeof module.updateConversionValue === "function") {
    bridge.updateConversionValue = async (fineValue, coarseValue) => {
      // Clamped here as well as server-side. Apple rejects the whole update for
      // an out-of-range value rather than clamping it, and the SDK cannot see
      // the rejection on iOS 14, so an out-of-range value would be lost in
      // silence.
      const clamped = Math.max(0, Math.min(63, Math.trunc(fineValue)));
      return (await module.updateConversionValue!(clamped, coarseValue)) === true;
    };
  }

  return bridge;
}
