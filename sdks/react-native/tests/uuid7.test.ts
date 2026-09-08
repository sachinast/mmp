/**
 * UUIDv7 minting.
 *
 * The timestamp encoding is the part worth testing hard: JavaScript's bitwise
 * operators truncate to 32 bits, so the obvious `now >> 40` produces the wrong
 * byte for every timestamp after 1970 — and the resulting ids still look
 * perfectly valid.
 */
import { describe, expect, it } from "vitest";

import { InsecureRandomError, platformRandom, uuid7 } from "../src/uuid7";

const ZEROS = { fill: (b: Uint8Array) => b.fill(0) };
const ONES = { fill: (b: Uint8Array) => b.fill(0xff) };

describe("format", () => {
  it("is a well-formed version 7 uuid", () => {
    const id = uuid7(ZEROS, 0x0123456789ab);
    expect(id).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
  });

  it("sets the version and variant even when every random bit is 1", () => {
    const id = uuid7(ONES, 0);
    expect(id[14]).toBe("7");
    expect("89ab").toContain(id[19]);
  });
});

describe("timestamp", () => {
  it("encodes all 48 bits, not the low 32", () => {
    // The bug this catches: `now >> 40` in JavaScript truncates to 32 bits and
    // silently returns 0 for the top bytes.
    const id = uuid7(ZEROS, 0x0123456789ab);
    expect(id.slice(0, 13).replace("-", "")).toBe("0123456789ab");
  });

  it("sorts in time order, which is the whole reason for v7", () => {
    const ids = [1_700_000_000_000, 1_700_000_000_001, 1_700_000_001_000].map((t) =>
      uuid7(ZEROS, t),
    );
    expect([...ids].sort()).toEqual(ids);
  });

  it("still encodes correctly well beyond 2038", () => {
    const year2100 = Date.UTC(2100, 0, 1);
    const id = uuid7(ZEROS, year2100);
    const encoded = parseInt(id.slice(0, 13).replace("-", ""), 16);
    expect(encoded).toBe(year2100);
  });
});

describe("randomness", () => {
  it("refuses to mint without a secure source", () => {
    // A weak anonymous_id merges two people's data; a weak event_id lets the
    // server's deduplication discard a real event. Both are silent, so this
    // fails loudly instead.
    const insecure = platformRandom.call(null);
    const saved = (globalThis as { crypto?: unknown }).crypto;
    try {
      delete (globalThis as { crypto?: unknown }).crypto;
      expect(() => uuid7(platformRandom(), 0)).toThrow(InsecureRandomError);
    } finally {
      (globalThis as { crypto?: unknown }).crypto = saved;
    }
    expect(insecure).toBeDefined();
  });

  it("uses the platform CSPRNG when there is one", () => {
    const id = uuid7(platformRandom(), Date.now());
    expect(id).toHaveLength(36);
  });

  it("does not repeat within a millisecond", () => {
    const random = platformRandom();
    const ids = new Set(Array.from({ length: 500 }, () => uuid7(random, 1_700_000_000_000)));
    expect(ids.size).toBe(500);
  });
});
