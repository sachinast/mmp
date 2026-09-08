/**
 * @mmp/react-native — mobile measurement for React Native.
 *
 * ```ts
 * import { MMP } from "@mmp/react-native";
 * import AsyncStorage from "@react-native-async-storage/async-storage";
 *
 * await MMP.initialize(
 *   { apiKey: "pk_live_...", endpoint: "https://track.example.com" },
 *   { storage: adaptKeyValueStore(AsyncStorage) },
 * );
 *
 * await MMP.track("purchase", { revenueMinor: 499, currency: "USD" });
 * ```
 *
 * The module-level `MMP` is a convenience for the overwhelmingly common case of
 * one app sending to one endpoint. `MmpClient` is exported for tests and for
 * anyone who needs two.
 */
import { MmpClient, type ClientDependencies } from "./client";
import type { MmpConfig } from "./types";

export { MmpClient } from "./client";
export {
  MemoryStorage,
  adaptKeyValueStore,
  type KeyValueStore,
  type Storage,
} from "./storage";
export { InsecureRandomError } from "./uuid7";
export { ConversionValues, COARSE_ORDER } from "./conversion";
export type { CoarseValue, ConversionMapping } from "./conversion";
export type { NativeBridge, DeviceInfo } from "./native";
export {
  createNativeBridge,
  isUsableAdvertisingId,
  type NativeModuleShape,
} from "./native-bridge";
export type {
  ConsentState,
  ConsentUpdate,
  EventProperties,
  Logger,
  MmpConfig,
  Purpose,
  TrackOptions,
} from "./types";

let instance: MmpClient | null = null;

function active(): MmpClient | null {
  if (!instance) {
    // Warned, never thrown. An app that forgot to initialise should lose its
    // analytics, not crash for its users.
    console.warn("mmp: not initialized — call MMP.initialize() first");
  }
  return instance;
}

export const MMP = {
  async initialize(config: MmpConfig, deps: ClientDependencies = {}): Promise<void> {
    if (instance) return;
    instance = new MmpClient(config, deps);
    await instance.initialize();
  },

  track: (name: string, options = {}) => active()?.track(name, options) ?? Promise.resolve(),
  setUserId: (userId: string | null) => active()?.setUserId(userId) ?? Promise.resolve(),
  setConsent: (update: Parameters<MmpClient["setConsent"]>[0]) =>
    active()?.setConsent(update) ?? Promise.resolve(),
  getDeferredDeepLink: () => active()?.getDeferredDeepLink() ?? Promise.resolve(null),
  flush: () => active()?.flush() ?? Promise.resolve(),
  reset: () => active()?.reset() ?? Promise.resolve(),

  async shutdown(): Promise<void> {
    await instance?.shutdown();
    instance = null;
  },
};
