/**
 * Consent, held on the device and reported to the server.
 *
 * The SDK's job is narrow and it matters that it stays narrow: it does not
 * decide what consent is required, does not show a dialogue, and does not
 * interpret a jurisdiction. The app tells it what the user chose, and it
 * reports that unchanged. Anything cleverer would put a legal judgement inside
 * a library that cannot see the context it is being made in.
 *
 * What it does enforce is local: when `advertising` is not granted, the
 * advertising ID is never requested from the platform in the first place. The
 * server also minimises on the way in, so this is the second of two
 * independent checks — but it is the one that stops the identifier ever being
 * read, which is stronger than not sending it.
 */
import { KEYS, type Storage } from "./storage";
import type { ConsentState, ConsentUpdate, Purpose } from "./types";

const PURPOSES: readonly Purpose[] = ["analytics", "attribution", "advertising"];

export class Consent {
  private states: Partial<Record<Purpose, ConsentState>> = {};

  constructor(private readonly storage: Storage) {}

  async load(): Promise<void> {
    const raw = await this.storage.get(KEYS.consent);
    if (!raw) return;
    try {
      const parsed = JSON.parse(raw) as ConsentUpdate;
      for (const purpose of PURPOSES) {
        const value = parsed[purpose];
        if (value === "granted" || value === "denied") this.states[purpose] = value;
      }
    } catch {
      // An unreadable consent record is treated as no record rather than as a
      // grant. The server's `strict` mode then denies, which is the safe way
      // for this failure to resolve.
      this.states = {};
    }
  }

  /** @returns the purposes that actually changed, so the SDK does not send a
   *  consent event every launch for a decision it already reported. */
  async update(update: ConsentUpdate): Promise<ConsentUpdate> {
    const changed: ConsentUpdate = {};
    for (const purpose of PURPOSES) {
      const value = update[purpose];
      if (value && this.states[purpose] !== value) {
        this.states[purpose] = value;
        changed[purpose] = value;
      }
    }
    if (Object.keys(changed).length > 0) {
      await this.storage.set(KEYS.consent, JSON.stringify(this.states));
    }
    return changed;
  }

  /** Granted only if explicitly granted. Unknown is not a yes. */
  allows(purpose: Purpose): boolean {
    return this.states[purpose] === "granted";
  }

  get known(): boolean {
    return Object.keys(this.states).length > 0;
  }

  snapshot(): ConsentUpdate {
    return { ...this.states };
  }
}
