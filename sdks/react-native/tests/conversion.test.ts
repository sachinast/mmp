/**
 * Conversion values.
 *
 * The rule that drives every test here: Apple *ignores* a decrease rather than
 * rejecting it. An SDK that calls on every event therefore looks like it is
 * working while silently discarding most of what it sends — and each call also
 * restarts Apple's measurement timer, so the chatty version delays the very
 * postback it is trying to enrich.
 */
import { describe, expect, it } from "vitest";

import { ConversionValues } from "../src/conversion";
import { MemoryStorage } from "../src/storage";

const MAPPINGS = [
  { event_name: "signup", conversion_value: 5 },
  { event_name: "add_to_cart", conversion_value: 20, coarse_value: "medium" as const },
  { event_name: "purchase", conversion_value: 40, coarse_value: "high" as const },
];

function build(storage = new MemoryStorage()) {
  const values = new ConversionValues(storage);
  values.setMappings(MAPPINGS);
  return { values, storage };
}

describe("mapping", () => {
  it("returns nothing for an event with no mapping", async () => {
    const { values } = build();
    expect(await values.apply("some_random_event")).toBeNull();
  });

  it("raises the value for a mapped event", async () => {
    const { values } = build();
    expect(await values.apply("signup")).toEqual({ fineValue: 5, coarseValue: null });
  });

  it("refuses a mapping outside Apple's six bits", async () => {
    // A server sending 64 would have Apple reject the whole update rather than
    // clamp it, so it never reaches the native call.
    const values = new ConversionValues(new MemoryStorage());
    values.setMappings([
      { event_name: "bad_high", conversion_value: 64 },
      { event_name: "bad_low", conversion_value: -1 },
      { event_name: "bad_float", conversion_value: 2.5 },
      { event_name: "good", conversion_value: 63 },
    ]);
    expect(await values.apply("bad_high")).toBeNull();
    expect(await values.apply("bad_low")).toBeNull();
    expect(await values.apply("bad_float")).toBeNull();
    expect(await values.apply("good")).toEqual({ fineValue: 63, coarseValue: null });
  });
});

describe("only ever upwards", () => {
  it("does not call again for a lower value", async () => {
    const { values } = build();
    await values.apply("purchase"); // 40
    expect(await values.apply("signup")).toBeNull(); // 5 — Apple would ignore it
    expect(values.current.fineValue).toBe(40);
  });

  it("does not call again for the same value", async () => {
    const { values } = build();
    await values.apply("signup");
    expect(await values.apply("signup")).toBeNull();
  });

  it("does call for a genuine increase", async () => {
    const { values } = build();
    await values.apply("signup");
    expect(await values.apply("purchase")).toEqual({ fineValue: 40, coarseValue: "high" });
  });

  it("raises the coarse value even when the fine value would not move", async () => {
    // SKAdNetwork 4: low-volume campaigns receive only the coarse value, so a
    // coarse increase is worth a call on its own.
    const values = new ConversionValues(new MemoryStorage());
    values.setMappings([
      { event_name: "a", conversion_value: 30, coarse_value: "low" },
      { event_name: "b", conversion_value: 10, coarse_value: "high" },
    ]);
    await values.apply("a");
    expect(await values.apply("b")).toEqual({ fineValue: 30, coarseValue: "high" });
  });

  it("never lowers the coarse value", async () => {
    const values = new ConversionValues(new MemoryStorage());
    values.setMappings([
      { event_name: "high", conversion_value: 10, coarse_value: "high" },
      { event_name: "later", conversion_value: 50, coarse_value: "low" },
    ]);
    await values.apply("high");
    const update = await values.apply("later");
    expect(update).toEqual({ fineValue: 50, coarseValue: "high" });
  });
});

describe("durability", () => {
  it("remembers the high-water mark across a restart", async () => {
    // Otherwise a relaunch re-sends a value Apple already has, restarting the
    // measurement timer for nothing.
    const storage = new MemoryStorage();
    const first = build(storage);
    await first.values.apply("purchase");

    const second = build(storage);
    expect(await second.values.apply("signup")).toBeNull();
    expect(await second.values.apply("purchase")).toBeNull();
  });

  it("recovers from a corrupt high-water mark", async () => {
    const storage = new MemoryStorage();
    await storage.set("mmp.consent.skan", "{{{not json");
    const { values } = build(storage);
    expect(await values.apply("signup")).toEqual({ fineValue: 5, coarseValue: null });
  });

  it("clears on reset", async () => {
    const { values } = build();
    await values.apply("purchase");
    await values.clear();
    expect(values.current).toEqual({ fineValue: 0, coarseValue: null });
  });
});
