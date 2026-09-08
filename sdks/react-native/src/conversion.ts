/**
 * SKAdNetwork conversion values.
 *
 * On iOS, a conversion value is the *only* thing an advertiser learns about
 * what someone did after installing: six bits, set before Apple's timer
 * expires, and then nothing. No event stream, no revenue figure. So the rules
 * around setting it are unusually unforgiving, and two of them are enforced
 * here because getting either wrong is silent.
 *
 * **Apple ignores a decrease.** `updatePostbackConversionValue` only ever moves
 * the value up. Calling it with a lower number is not an error and not a
 * rollback — it simply does nothing. An SDK that maps events to values and
 * calls on every event will therefore appear to work while quietly discarding
 * most of what it sends, so this tracks the high-water mark and only calls when
 * the value would actually change.
 *
 * **The timer restarts on every call.** Each update extends the measurement
 * window, which is useful when it is intended and expensive when it is not: a
 * chatty app that calls on every event delays its own postback. Calling only on
 * a genuine increase is what keeps that from happening by accident.
 *
 * The mapping itself comes from the server, because deciding what the 64 values
 * mean is the advertiser's modelling decision and it changes without an app
 * release.
 */
import { KEYS, type Storage } from "./storage";

/** SKAdNetwork 4 coarse values, in Apple's order. */
export const COARSE_ORDER = ["low", "medium", "high"] as const;
export type CoarseValue = (typeof COARSE_ORDER)[number];

export interface ConversionMapping {
  event_name: string;
  conversion_value: number;
  coarse_value?: CoarseValue | null;
}

export interface ConversionUpdate {
  fineValue: number;
  coarseValue: CoarseValue | null;
}

interface StoredState {
  fine: number;
  coarse: CoarseValue | null;
}

const STORAGE_KEY = `${KEYS.consent}.skan`;

/**
 * Decides whether an event should move the conversion value, and to what.
 *
 * Deliberately pure of any native call: what to send is a decision worth
 * testing on its own, separately from the platform API that sends it.
 */
export class ConversionValues {
  private mappings = new Map<string, ConversionMapping>();
  private state: StoredState = { fine: 0, coarse: null };
  private loaded = false;

  constructor(private readonly storage: Storage) {}

  async load(): Promise<void> {
    if (this.loaded) return;
    this.loaded = true;
    const raw = await this.storage.get(STORAGE_KEY);
    if (!raw) return;
    try {
      const parsed = JSON.parse(raw) as StoredState;
      if (typeof parsed.fine === "number") this.state = parsed;
    } catch {
      // A corrupt high-water mark resets to zero. The cost is one redundant
      // update; the alternative is never updating again on this install.
      this.state = { fine: 0, coarse: null };
    }
  }

  setMappings(mappings: readonly ConversionMapping[]): void {
    this.mappings = new Map();
    for (const mapping of mappings) {
      if (!Number.isInteger(mapping.conversion_value)) continue;
      // Six bits. A server that sends something outside the range is not
      // trusted into the native call, where Apple would reject the whole
      // update rather than clamp it.
      if (mapping.conversion_value < 0 || mapping.conversion_value > 63) continue;
      this.mappings.set(mapping.event_name, mapping);
    }
  }

  get current(): ConversionUpdate {
    return { fineValue: this.state.fine, coarseValue: this.state.coarse };
  }

  /**
   * @returns what to send to Apple, or null when this event would not move the
   *   value — either because it is unmapped, or because Apple would ignore it.
   */
  async apply(eventName: string): Promise<ConversionUpdate | null> {
    await this.load();
    const mapping = this.mappings.get(eventName);
    if (!mapping) return null;

    const coarse = mapping.coarse_value ?? null;
    const fineRises = mapping.conversion_value > this.state.fine;
    const coarseRises = coarse !== null && rank(coarse) > rank(this.state.coarse);
    if (!fineRises && !coarseRises) return null;

    this.state = {
      fine: Math.max(this.state.fine, mapping.conversion_value),
      // Never lowered, for the same reason the fine value is not: Apple would
      // ignore the decrease, so tracking it locally would put our idea of the
      // value out of step with Apple's.
      coarse: coarseRises ? coarse : this.state.coarse,
    };
    await this.storage.set(STORAGE_KEY, JSON.stringify(this.state));
    return { fineValue: this.state.fine, coarseValue: this.state.coarse };
  }

  /** For `reset()`, which severs a new identity from the old one. */
  async clear(): Promise<void> {
    this.state = { fine: 0, coarse: null };
    await this.storage.remove(STORAGE_KEY);
  }
}

function rank(value: CoarseValue | null): number {
  return value === null ? -1 : COARSE_ORDER.indexOf(value);
}
