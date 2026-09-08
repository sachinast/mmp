/**
 * UUIDv7 (RFC 9562), minted on the device.
 *
 * The same identifier format the backend uses, for the same reason: the first
 * 48 bits are a millisecond timestamp, so ids generated near each other in time
 * sort near each other, and the rows they become land near each other in an
 * index instead of scattering across it.
 *
 * Two of these matter more than the rest:
 *
 * - `event_id` is the idempotency key. The server deduplicates on it, so an
 *   event that is retried after an ambiguous failure is stored once. That only
 *   works if the id is minted before the first attempt and kept across retries
 *   and app restarts — never regenerated.
 * - `anonymous_id` is the device's identity. A collision here does not lose
 *   data, it *merges two people*, so this module refuses to mint one without a
 *   real source of randomness rather than quietly falling back to Math.random.
 */

/** Injected in tests; in an app this is the platform's CSPRNG. */
export interface RandomSource {
  fill(bytes: Uint8Array): void;
}

export class InsecureRandomError extends Error {
  constructor() {
    super(
      "no cryptographically secure random source is available. Install " +
        "react-native-get-random-values (or another polyfill for " +
        "crypto.getRandomValues) before initialising the SDK.",
    );
    this.name = "InsecureRandomError";
  }
}

/**
 * The platform's CSPRNG, or a thrower.
 *
 * Deliberately not falling back to Math.random. A weak `anonymous_id` merges
 * two users' data into one person's profile, and a weak `event_id` lets the
 * server's deduplication drop a real event as a duplicate of an unrelated one.
 * Both are silent, and both are worse than an SDK that refuses to start and
 * says why.
 */
export function platformRandom(): RandomSource {
  // Typed structurally rather than as `Crypto`: React Native's global is
  // supplied by a polyfill, not by lib.dom, so the DOM type is not present.
  const globalCrypto = (
    globalThis as { crypto?: { getRandomValues?: (a: Uint8Array) => Uint8Array } }
  ).crypto;
  if (globalCrypto && typeof globalCrypto.getRandomValues === "function") {
    return {
      fill(bytes: Uint8Array) {
        globalCrypto.getRandomValues!(bytes);
      },
    };
  }
  return {
    fill() {
      throw new InsecureRandomError();
    },
  };
}

const HEX = Array.from({ length: 256 }, (_, i) => i.toString(16).padStart(2, "0"));

/**
 * @param now  milliseconds since the epoch — a parameter so tests can pin it.
 */
export function uuid7(random: RandomSource, now: number = Date.now()): string {
  const bytes = new Uint8Array(16);
  random.fill(bytes);

  // 48-bit big-endian timestamp. Written with division rather than bit shifts:
  // JavaScript's bitwise operators truncate to 32 bits, so `now >> 40` silently
  // produces the wrong byte for every timestamp after 1970.
  const ms = Math.floor(now);
  bytes[0] = Math.floor(ms / 2 ** 40) & 0xff;
  bytes[1] = Math.floor(ms / 2 ** 32) & 0xff;
  bytes[2] = Math.floor(ms / 2 ** 24) & 0xff;
  bytes[3] = Math.floor(ms / 2 ** 16) & 0xff;
  bytes[4] = Math.floor(ms / 2 ** 8) & 0xff;
  bytes[5] = ms & 0xff;

  // Version 7 in the high nibble of byte 6, variant 0b10 in the top bits of
  // byte 8. The remaining 74 bits stay random.
  bytes[6] = ((bytes[6] as number) & 0x0f) | 0x70;
  bytes[8] = ((bytes[8] as number) & 0x3f) | 0x80;

  const h = (i: number) => HEX[bytes[i] as number] as string;
  return (
    h(0) + h(1) + h(2) + h(3) + "-" +
    h(4) + h(5) + "-" +
    h(6) + h(7) + "-" +
    h(8) + h(9) + "-" +
    h(10) + h(11) + h(12) + h(13) + h(14) + h(15)
  );
}
