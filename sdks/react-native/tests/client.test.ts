/**
 * The client's contract with the app that embeds it.
 *
 * The recurring theme: an SDK failure must degrade measurement, never the host
 * app. Several of these tests assert that something *does not* happen — no
 * throw, no identifier read, no duplicate install.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MmpClient } from "../src/client";
import { MemoryStorage, KEYS } from "../src/storage";
import type { NativeBridge } from "../src/native";
import type { MmpConfig, WireEvent } from "../src/types";

/**
 * A counter, not a constant. A fixed filler plus a fixed clock makes UUIDv7
 * fully deterministic, which would make `reset()` appear to mint the same id
 * twice — hiding the very thing that test exists to check. A real CSPRNG varies
 * every call and so does this.
 */
let counter = 0;
const RANDOM = {
  fill: (bytes: Uint8Array) => {
    counter += 1;
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = (counter * 31 + i) & 0xff;
  },
};

interface Harness {
  client: MmpClient;
  storage: MemoryStorage;
  sent: WireEvent[];
  bodies: unknown[];
}

function harness(
  config: Partial<MmpConfig> = {},
  native?: NativeBridge,
  storage = new MemoryStorage(),
): Harness {
  const sent: WireEvent[] = [];
  const bodies: unknown[] = [];
  const fetchImpl = (async (_url: string, init: { body: string }) => {
    const body = JSON.parse(init.body) as { events?: WireEvent[] };
    bodies.push(body);
    if (body.events) sent.push(...body.events);
    return { status: 202, ok: true, json: async () => ({}) };
  }) as unknown as typeof fetch;

  const client = new MmpClient(
    { apiKey: "pk_test", endpoint: "https://track.example.com", ...config },
    { storage, random: RANDOM, fetchImpl, now: () => 1_700_000_000_000, ...(native ? { native } : {}) },
  );
  return { client, storage, sent, bodies };
}

describe("configuration", () => {
  it("refuses plain http outside local development", () => {
    // Over HTTP the api key is readable by anyone on the network.
    expect(
      () => new MmpClient({ apiKey: "k", endpoint: "http://track.example.com" }),
    ).toThrow(/https/);
  });

  it("allows http against localhost so a simulator can be pointed at a laptop", () => {
    expect(
      () => new MmpClient({ apiKey: "k", endpoint: "http://localhost:8000" }),
    ).not.toThrow();
  });

  it("requires an api key and an endpoint", () => {
    expect(() => new MmpClient({ apiKey: "", endpoint: "https://x.example" })).toThrow(/apiKey/);
    expect(() => new MmpClient({ apiKey: "k", endpoint: "" })).toThrow(/endpoint/);
  });
});

describe("install reporting", () => {
  it("sends exactly one install on first launch", async () => {
    const h = harness();
    await h.client.initialize();
    await h.client.flush();
    expect(h.sent.filter((e) => e.event_name === "install")).toHaveLength(1);
  });

  it("does not send another install on the next launch", async () => {
    const storage = new MemoryStorage();
    const first = harness({}, undefined, storage);
    await first.client.initialize();
    await first.client.flush();

    const second = harness({}, undefined, storage);
    await second.client.initialize();
    await second.client.flush();

    expect(second.sent.filter((e) => e.event_name === "install")).toHaveLength(0);
  });

  it("does not invent installs for users who predate the SDK", async () => {
    // An existing device with no install flag means the SDK was added to an app
    // that already had users. Counting them as installs would fabricate a spike.
    const storage = new MemoryStorage();
    await storage.set(KEYS.anonymousId, "pre-existing-device");

    const h = harness({}, undefined, storage);
    await h.client.initialize();
    await h.client.flush();

    expect(h.sent.filter((e) => e.event_name === "install")).toHaveLength(0);
    expect(await storage.get(KEYS.installReported)).toBe("existing");
  });

  it("carries the install referrer when the native module supplies one", async () => {
    const native: NativeBridge = {
      async getInstallReferrer() {
        return "utm_content=abc&utm_source=net";
      },
    };
    const h = harness({}, native);
    await h.client.initialize();
    await h.client.flush();

    const install = h.sent.find((e) => e.event_name === "install");
    expect(install?.properties?.["install_referrer"]).toBe("utm_content=abc&utm_source=net");
  });
});

describe("consent", () => {
  it("never asks the platform for an advertising id before consent", async () => {
    const getAdvertisingId = vi.fn(async () => "raw-gaid-value");
    const h = harness({}, { getAdvertisingId });

    await h.client.initialize();

    expect(getAdvertisingId).not.toHaveBeenCalled();
  });

  it("asks for it once attribution consent is granted", async () => {
    const getAdvertisingId = vi.fn(async () => "raw-gaid-value");
    const storage = new MemoryStorage();
    await storage.set(KEYS.consent, JSON.stringify({ attribution: "granted" }));

    const h = harness({}, { getAdvertisingId }, storage);
    await h.client.initialize();

    expect(getAdvertisingId).toHaveBeenCalled();
  });

  it("reports a consent decision as an event", async () => {
    const h = harness();
    await h.client.initialize();
    await h.client.setConsent({ analytics: "granted", advertising: "denied" });
    await h.client.flush();

    const event = h.sent.find((e) => e.event_name === "consent_update");
    expect(event?.properties).toEqual({ analytics: "granted", advertising: "denied" });
  });

  it("does not re-report a decision that has not changed", async () => {
    const h = harness();
    await h.client.initialize();
    await h.client.setConsent({ analytics: "granted" });
    await h.client.setConsent({ analytics: "granted" });
    await h.client.flush();

    expect(h.sent.filter((e) => e.event_name === "consent_update")).toHaveLength(1);
  });

  it("can hold every event until consent is known", async () => {
    const h = harness({ trackWithoutConsent: false });
    await h.client.initialize();
    await h.client.track("purchase");
    await h.client.flush();

    expect(h.sent.filter((e) => e.event_name === "purchase")).toHaveLength(0);
  });
});

describe("track", () => {
  it("refuses a reserved event name", async () => {
    const h = harness();
    await h.client.initialize();
    await h.client.track("install");
    await h.client.flush();

    expect(h.sent.filter((e) => e.event_name === "install")).toHaveLength(1); // the SDK's own
  });

  it("refuses revenue without a currency", async () => {
    const h = harness();
    await h.client.initialize();
    await h.client.track("purchase", { revenueMinor: 499 });
    await h.client.flush();
    expect(h.sent.find((e) => e.event_name === "purchase")).toBeUndefined();
  });

  it("refuses fractional money", async () => {
    // Money as a float is how a revenue report stops reconciling.
    const h = harness();
    await h.client.initialize();
    await h.client.track("purchase", { revenueMinor: 4.99, currency: "USD" });
    await h.client.flush();
    expect(h.sent.find((e) => e.event_name === "purchase")).toBeUndefined();
  });

  it("accepts a well-formed purchase", async () => {
    const h = harness();
    await h.client.initialize();
    await h.client.track("purchase", { revenueMinor: 499, currency: "USD" });
    await h.client.flush();

    const event = h.sent.find((e) => e.event_name === "purchase");
    expect(event?.revenue_minor).toBe(499);
    expect(event?.currency).toBe("USD");
    expect(event?.session_id).toBeTruthy();
  });

  it("attaches the user id once one is set, and links the device", async () => {
    const h = harness();
    await h.client.initialize();
    await h.client.setUserId("user-42");
    await h.client.track("purchase");
    await h.client.flush();

    expect(h.sent.find((e) => e.event_name === "login")).toBeTruthy();
    expect(h.sent.find((e) => e.event_name === "purchase")?.user_id).toBe("user-42");
  });
});

describe("failure containment", () => {
  it("does not throw when storage is broken", async () => {
    const broken = {
      async get() {
        throw new Error("disk full");
      },
      async set() {
        throw new Error("disk full");
      },
      async remove() {
        throw new Error("disk full");
      },
    };
    const client = new MmpClient(
      { apiKey: "k", endpoint: "https://track.example.com" },
      { storage: broken, random: RANDOM, fetchImpl: (async () => ({ status: 202, ok: true })) as unknown as typeof fetch },
    );

    await expect(client.initialize()).resolves.toBeUndefined();
    await expect(client.track("purchase")).resolves.toBeUndefined();
  });

  it("does not throw when the network is down", async () => {
    const failing = (async () => {
      throw new Error("network down");
    }) as unknown as typeof fetch;
    const client = new MmpClient(
      { apiKey: "k", endpoint: "https://track.example.com" },
      { storage: new MemoryStorage(), random: RANDOM, fetchImpl: failing },
    );

    await client.initialize();
    await expect(client.track("purchase")).resolves.toBeUndefined();
    await expect(client.flush()).resolves.toBeUndefined();
  });

  it("does not throw when the native module misbehaves", async () => {
    const hostile: NativeBridge = {
      async getInstallReferrer() {
        throw new Error("no play services");
      },
      async getDeviceInfo() {
        throw new Error("boom");
      },
    };
    const h = harness({}, hostile);
    await expect(h.client.initialize()).resolves.toBeUndefined();
  });
});

describe("reset", () => {
  it("severs the new identity from the old one", async () => {
    const h = harness();
    await h.client.initialize();
    await h.client.track("before");
    await h.client.flush();
    const before = h.sent.find((e) => e.event_name === "before")?.anonymous_id;

    await h.client.reset();
    await h.client.track("after");
    await h.client.flush();
    const after = h.sent.find((e) => e.event_name === "after")?.anonymous_id;

    expect(before).toBeTruthy();
    expect(after).toBeTruthy();
    expect(after).not.toBe(before);
  });
});

beforeEach(() => {
  counter = 0;
  vi.restoreAllMocks();
});


describe("skadnetwork conversion values", () => {
  function iosHarness(mappings: unknown[], updateConversionValue = vi.fn(async () => true)) {
    const storage = new MemoryStorage();
    const calls: string[] = [];
    const fetchImpl = (async (url: string, init?: { body?: string }) => {
      calls.push(url);
      if (url.includes("/v1/skan/conversion-values")) {
        return { status: 200, ok: true, json: async () => ({ mappings }) };
      }
      void init;
      return { status: 202, ok: true, json: async () => ({}) };
    }) as unknown as typeof fetch;

    const client = new MmpClient(
      { apiKey: "pk", endpoint: "https://track.example.com" },
      {
        storage,
        random: RANDOM,
        fetchImpl,
        now: () => 1_700_000_000_000,
        native: {
          async getDeviceInfo() {
            return { platform: "ios" as const, osVersion: "17.0" };
          },
          updateConversionValue,
        },
      },
    );
    return { client, updateConversionValue, calls };
  }

  it("reports a conversion value for a mapped event", async () => {
    const h = iosHarness([{ event_name: "purchase", conversion_value: 40, coarse_value: "high" }]);
    await h.client.initialize();
    await h.client.track("purchase");

    expect(h.updateConversionValue).toHaveBeenCalledWith(40, "high");
  });

  it("does not call Apple again for a value that would be ignored", async () => {
    // Apple ignores a decrease rather than reporting one, and every accepted
    // call restarts the measurement window — so a chatty SDK delays its own
    // postback while appearing to work.
    const h = iosHarness([
      { event_name: "purchase", conversion_value: 40 },
      { event_name: "signup", conversion_value: 5 },
    ]);
    await h.client.initialize();
    await h.client.track("purchase");
    await h.client.track("signup");
    await h.client.track("purchase");

    expect(h.updateConversionValue).toHaveBeenCalledTimes(1);
  });

  it("does nothing for an unmapped event", async () => {
    const h = iosHarness([{ event_name: "purchase", conversion_value: 40 }]);
    await h.client.initialize();
    await h.client.track("browse");
    expect(h.updateConversionValue).not.toHaveBeenCalled();
  });

  it("does not fetch the mapping on android", async () => {
    // SKAdNetwork is iOS-only; asking for it anywhere else is a request per
    // launch that can never be useful.
    const urls: string[] = [];
    const fetchImpl = (async (url: string) => {
      urls.push(url);
      return { status: 202, ok: true, json: async () => ({}) };
    }) as unknown as typeof fetch;

    const client = new MmpClient(
      { apiKey: "pk", endpoint: "https://track.example.com" },
      {
        storage: new MemoryStorage(),
        random: RANDOM,
        fetchImpl,
        native: {
          async getDeviceInfo() {
            return { platform: "android" as const };
          },
        },
      },
    );
    await client.initialize();
    await client.track("purchase");
    await client.flush();

    expect(urls.some((u) => u.includes("conversion-values"))).toBe(false);
    expect(urls.some((u) => u.includes("/v1/events"))).toBe(true);
  });

  it("keeps working when the mapping cannot be fetched", async () => {
    const failing = (async (url: string) => {
      if (url.includes("conversion-values")) throw new Error("offline");
      return { status: 202, ok: true, json: async () => ({}) };
    }) as unknown as typeof fetch;

    const client = new MmpClient(
      { apiKey: "pk", endpoint: "https://track.example.com" },
      {
        storage: new MemoryStorage(),
        random: RANDOM,
        fetchImpl: failing,
        native: {
          async getDeviceInfo() {
            return { platform: "ios" as const };
          },
        },
      },
    );
    await expect(client.initialize()).resolves.toBeUndefined();
    await expect(client.track("purchase")).resolves.toBeUndefined();
  });
});
