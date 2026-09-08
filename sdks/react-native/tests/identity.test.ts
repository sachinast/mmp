/** Device identity and sessions. */
import { describe, expect, it } from "vitest";

import { Identity, SESSION_TIMEOUT_MS } from "../src/identity";
import { KEYS, MemoryStorage } from "../src/storage";

let counter = 0;
const RANDOM = {
  fill: (bytes: Uint8Array) => {
    counter += 1;
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = (counter * 17 + i) & 0xff;
  },
};

describe("anonymous id", () => {
  it("is minted once and reused on every later launch", async () => {
    const storage = new MemoryStorage();

    const first = new Identity(storage, RANDOM);
    expect((await first.load(1000)).firstLaunch).toBe(true);
    const id = first.deviceId;

    const second = new Identity(storage, RANDOM);
    expect((await second.load(2000)).firstLaunch).toBe(false);
    expect(second.deviceId).toBe(id);
  });

  it("is not derived from anything about the device", async () => {
    // It is a random value that means nothing outside this app, which is what
    // makes it safe to hold without consent. Two devices must never agree.
    const a = new Identity(new MemoryStorage(), RANDOM);
    const b = new Identity(new MemoryStorage(), RANDOM);
    await a.load(1000);
    await b.load(1000);
    expect(a.deviceId).not.toBe(b.deviceId);
  });
});

describe("sessions", () => {
  it("keeps one session across close activity", async () => {
    const identity = new Identity(new MemoryStorage(), RANDOM);
    await identity.load(0);
    const first = await identity.touchSession(0);
    const later = await identity.touchSession(SESSION_TIMEOUT_MS - 1);
    expect(later).toBe(first);
  });

  it("starts a new session after the timeout", async () => {
    const identity = new Identity(new MemoryStorage(), RANDOM);
    await identity.load(0);
    const first = await identity.touchSession(0);
    const later = await identity.touchSession(SESSION_TIMEOUT_MS + 1);
    expect(later).not.toBe(first);
  });

  it("measures inactivity from the last event, not from the session start", async () => {
    const identity = new Identity(new MemoryStorage(), RANDOM);
    await identity.load(0);
    const first = await identity.touchSession(0);
    // Someone using the app steadily for two hours is in one session.
    let last = first;
    for (let t = 0; t <= 2 * 60 * 60 * 1000; t += SESSION_TIMEOUT_MS - 60_000) {
      last = await identity.touchSession(t);
    }
    expect(last).toBe(first);
  });

  it("survives a restart", async () => {
    const storage = new MemoryStorage();
    const first = new Identity(storage, RANDOM);
    await first.load(0);
    const session = await first.touchSession(0);

    const second = new Identity(storage, RANDOM);
    await second.load(1000);
    expect(await second.touchSession(1000)).toBe(session);
  });
});

describe("reset", () => {
  it("replaces the device identity and forgets the user", async () => {
    const storage = new MemoryStorage();
    const identity = new Identity(storage, RANDOM);
    await identity.load(0);
    await identity.setUserId("user-1");
    const before = identity.deviceId;

    await identity.reset(1000);

    expect(identity.deviceId).not.toBe(before);
    expect(identity.user).toBeNull();
    expect(await storage.get(KEYS.userId)).toBeNull();
  });
});
