/**
 * The native bridge wrapper.
 *
 * The opt-out cases carry the weight here. Both platforms answer "the user said
 * no" by returning the all-zero UUID rather than by failing, so treating that
 * as a real identifier would send one shared value for every opted-out device —
 * making them all match each other, which is precisely what the opt-out exists
 * to prevent.
 */
import { describe, expect, it, vi } from "vitest";

import { createNativeBridge, isUsableAdvertisingId } from "../src/native-bridge";
import { callNative } from "../src/native";

describe("opted-out advertising ids", () => {
  it.each([
    "00000000-0000-0000-0000-000000000000",
    "00000000-0000-0000-0000-000000000000".toUpperCase(),
    "",
    "   ",
    "null",
    "unknown",
    "NONE",
  ])("treats %o as no identifier", (value) => {
    expect(isUsableAdvertisingId(value)).toBe(false);
  });

  it.each([null, undefined, 0, {}, []])("treats the non-string %o as none", (value) => {
    expect(isUsableAdvertisingId(value)).toBe(false);
  });

  it("accepts a real identifier", () => {
    expect(isUsableAdvertisingId("38400000-8cf0-11bd-b23e-10b96e40000d")).toBe(true);
  });

  it("returns null from the bridge rather than the zero uuid", async () => {
    const bridge = createNativeBridge({
      async getAdvertisingId() {
        return "00000000-0000-0000-0000-000000000000";
      },
    });
    expect(await bridge.getAdvertisingId!()).toBeNull();
  });
});

describe("absence", () => {
  it("accepts no native module at all", async () => {
    const bridge = createNativeBridge(undefined);
    expect(bridge.getAdvertisingId).toBeUndefined();
    // The client's caller tolerates the absence rather than branching on it.
    expect(await callNative(bridge.getInstallReferrer, null)).toBeNull();
  });

  it("accepts a module that implements only some methods", async () => {
    const bridge = createNativeBridge({
      async getInstallReferrer() {
        return "utm_content=abc";
      },
    });
    expect(await bridge.getInstallReferrer!()).toBe("utm_content=abc");
    expect(bridge.getAdvertisingId).toBeUndefined();
  });
});

describe("device info", () => {
  it("passes through only fields the server accepts", async () => {
    // A native module can return anything. Spreading it would put an invented
    // field into the payload, where the server rejects unknown fields and the
    // whole batch fails.
    const bridge = createNativeBridge({
      async getDeviceInfo() {
        return {
          platform: "android",
          osVersion: "14",
          appVersion: "2.1.0",
          deviceModel: "Pixel 8",
          imei: "should-never-appear",
          location: { lat: 1, lon: 2 },
        };
      },
    });

    expect(await bridge.getDeviceInfo!()).toEqual({
      platform: "android",
      osVersion: "14",
      appVersion: "2.1.0",
      deviceModel: "Pixel 8",
    });
  });

  it("rejects a platform value that is not one of ours", async () => {
    const bridge = createNativeBridge({
      async getDeviceInfo() {
        return { platform: "harmonyos", osVersion: "4" };
      },
    });
    expect(await bridge.getDeviceInfo!()).toEqual({ osVersion: "4" });
  });

  it("tolerates a module returning nothing", async () => {
    const bridge = createNativeBridge({
      async getDeviceInfo() {
        return undefined as unknown as Record<string, unknown>;
      },
    });
    expect(await bridge.getDeviceInfo!()).toEqual({});
  });
});

describe("misbehaving native code", () => {
  it("a rejection becomes the fallback, not a crash", async () => {
    const bridge = createNativeBridge({
      async getAdvertisingId() {
        throw new Error("play services missing");
      },
    });
    expect(await callNative(bridge.getAdvertisingId, null)).toBeNull();
  });

  it("a call that never settles is bounded", async () => {
    // Real behaviour on Android without Play Services: the promise simply never
    // resolves. Without a timeout, initialize() would hang forever.
    vi.useFakeTimers();
    try {
      const bridge = createNativeBridge({
        getAdvertisingId: () => new Promise<string>(() => {}),
      });
      const pending = callNative(bridge.getAdvertisingId, null, 3_000);
      await vi.advanceTimersByTimeAsync(3_100);
      expect(await pending).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
