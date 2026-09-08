/**
 * The queue's invariants: never lose an event, never send one twice, never
 * grow without bound, never retry something the server has refused.
 */
import { describe, expect, it } from "vitest";

import { EventQueue } from "../src/queue";
import { MemoryStorage, KEYS } from "../src/storage";
import type { SendOutcome, Transport } from "../src/transport";
import type { WireEvent } from "../src/types";

function event(id: string, name = "purchase"): WireEvent {
  return {
    event_id: id,
    event_name: name,
    anonymous_id: "device-1",
    occurred_at: new Date(0).toISOString(),
  };
}

/** A transport that records what it was asked to send and replays scripted
 *  outcomes, so a test can describe a failure sequence exactly. */
class FakeTransport {
  readonly batches: WireEvent[][] = [];
  constructor(private readonly outcomes: SendOutcome[] = []) {}

  async send(events: WireEvent[]): Promise<SendOutcome> {
    this.batches.push([...events]);
    return this.outcomes.shift() ?? { kind: "accepted" };
  }

  get sentIds(): string[] {
    return this.batches.flat().map((e) => e.event_id);
  }

  asTransport(): Transport {
    return this as unknown as Transport;
  }
}

function build(transport: FakeTransport, overrides = {}) {
  const storage = new MemoryStorage();
  const queue = new EventQueue({
    storage,
    transport: transport.asTransport(),
    batchSize: 2,
    maxQueueSize: 5,
    random: () => 0.5,
    ...overrides,
  });
  return { queue, storage };
}

describe("delivery", () => {
  it("sends queued events and empties the queue", async () => {
    const transport = new FakeTransport();
    const { queue } = build(transport);

    await queue.enqueue(event("a"));
    await queue.enqueue(event("b"));
    expect(await queue.flush(0)).toBe(true);

    expect(transport.sentIds).toEqual(["a", "b"]);
    expect(queue.size).toBe(0);
  });

  it("drains a backlog larger than one batch in a single flush", async () => {
    const transport = new FakeTransport();
    const { queue } = build(transport);

    for (const id of ["a", "b", "c", "d", "e"]) await queue.enqueue(event(id));
    await queue.flush(0);

    expect(queue.size).toBe(0);
    expect(transport.batches.length).toBe(3);
  });

  it("keeps events when the outcome is ambiguous", async () => {
    const transport = new FakeTransport([{ kind: "retry", reason: "timeout" }]);
    const { queue } = build(transport);

    await queue.enqueue(event("a"));
    expect(await queue.flush(0)).toBe(false);
    expect(queue.size).toBe(1);
  });

  it("does not regenerate event ids across a retry", async () => {
    // The property the server's deduplication depends on. If a retry minted a
    // new id, one purchase would be stored twice and counted twice.
    const transport = new FakeTransport([{ kind: "retry", reason: "timeout" }]);
    const { queue } = build(transport);

    await queue.enqueue(event("stable-id"));
    await queue.flush(0);
    await queue.flush(999_999);

    expect(transport.batches.length).toBe(2);
    expect(transport.batches[0]![0]!.event_id).toBe("stable-id");
    expect(transport.batches[1]![0]!.event_id).toBe("stable-id");
  });

  it("drops events the server has refused rather than retrying forever", async () => {
    const transport = new FakeTransport([
      { kind: "rejected", reason: "http 422", status: 422 },
    ]);
    const { queue } = build(transport);

    await queue.enqueue(event("bad"));
    await queue.flush(0);

    expect(queue.size).toBe(0);
    expect(queue.stats.rejected).toBe(1);
  });

  it("does not let a rejected batch block the events behind it", async () => {
    const transport = new FakeTransport([
      { kind: "rejected", reason: "http 422", status: 422 },
      { kind: "accepted" },
    ]);
    const { queue } = build(transport);

    for (const id of ["bad1", "bad2", "good1", "good2"]) await queue.enqueue(event(id));
    await queue.flush(0);

    expect(queue.size).toBe(0);
    expect(transport.sentIds).toEqual(["bad1", "bad2", "good1", "good2"]);
  });
});

describe("backoff", () => {
  it("refuses to send again before the backoff expires", async () => {
    const transport = new FakeTransport([{ kind: "retry", reason: "5xx" }]);
    const { queue } = build(transport);

    await queue.enqueue(event("a"));
    await queue.flush(0);
    const attempts = transport.batches.length;

    await queue.flush(1);
    expect(transport.batches.length).toBe(attempts);
  });

  it("honours retry-after when the server sets one", async () => {
    const transport = new FakeTransport([
      { kind: "retry", reason: "429", retryAfterMs: 60_000 },
    ]);
    const { queue } = build(transport);

    await queue.enqueue(event("a"));
    await queue.flush(0);

    await queue.flush(59_000);
    expect(transport.batches.length).toBe(1);
    await queue.flush(60_000);
    expect(transport.batches.length).toBe(2);
  });

  it("jitters the backoff so clients do not retry in lockstep", async () => {
    // Every install that was offline during an outage comes back at once.
    // Without jitter they retry together and the recovering server is knocked
    // over by its own clients.
    const delays = new Set<number>();
    for (const r of [0.1, 0.5, 0.9]) {
      const transport = new FakeTransport([{ kind: "retry", reason: "5xx" }]);
      const { queue } = build(transport, { random: () => r });
      await queue.enqueue(event("a"));
      await queue.flush(0);
      // Probe when it becomes willing to send again.
      let delay = 0;
      while (delay < 5_000 && transport.batches.length === 1) {
        delay += 1;
        await queue.flush(delay);
      }
      delays.add(delay);
    }
    expect(delays.size).toBeGreaterThan(1);
  });

  it("resets the backoff after a success", async () => {
    const transport = new FakeTransport([
      { kind: "retry", reason: "5xx" },
      { kind: "accepted" },
    ]);
    const { queue } = build(transport);

    await queue.enqueue(event("a"));
    await queue.flush(0);
    await queue.flush(999_999);
    expect(queue.size).toBe(0);

    await queue.enqueue(event("b"));
    expect(await queue.flush(999_999)).toBe(true);
  });
});

describe("durability", () => {
  it("persists an event before any attempt to send it", async () => {
    const transport = new FakeTransport();
    const { queue, storage } = build(transport);

    await queue.enqueue(event("a"));

    const stored = await storage.get(KEYS.queue);
    expect(stored).toContain("a");
  });

  it("recovers queued events after a restart", async () => {
    const storage = new MemoryStorage();
    const first = new EventQueue({
      storage,
      transport: new FakeTransport([{ kind: "retry", reason: "offline" }]).asTransport(),
      batchSize: 2,
      maxQueueSize: 5,
    });
    await first.enqueue(event("survivor"));
    await first.flush(0);

    const transport = new FakeTransport();
    const second = new EventQueue({
      storage,
      transport: transport.asTransport(),
      batchSize: 2,
      maxQueueSize: 5,
    });
    await second.flush(999_999);

    expect(transport.sentIds).toEqual(["survivor"]);
  });

  it("discards a corrupt queue instead of failing forever", async () => {
    const storage = new MemoryStorage();
    await storage.set(KEYS.queue, "{not json");
    const transport = new FakeTransport();
    const queue = new EventQueue({
      storage,
      transport: transport.asTransport(),
      batchSize: 2,
      maxQueueSize: 5,
    });

    await queue.enqueue(event("after-corruption"));
    await queue.flush(0);
    expect(transport.sentIds).toEqual(["after-corruption"]);
  });
});

describe("bounds", () => {
  it("drops the oldest events when full, and says so", async () => {
    const transport = new FakeTransport();
    const { queue } = build(transport);

    for (const id of ["1", "2", "3", "4", "5", "6", "7"]) await queue.enqueue(event(id));

    expect(queue.size).toBe(5);
    expect(queue.peek().map((e) => e.event_id)).toEqual(["3", "4", "5", "6", "7"]);
    expect(queue.stats.dropped).toBe(2);
  });
});
