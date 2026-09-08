/**
 * Who this device is, and who is using it.
 *
 * Two identifiers, deliberately separate:
 *
 * - `anonymous_id` is the device. Minted on first launch, persisted, and never
 *   derived from anything about the hardware. It is not a device fingerprint
 *   and not an advertising ID — it is a random value that means nothing outside
 *   this app, which is what makes it safe to keep without consent.
 * - `user_id` is the app's own identifier for the person, supplied by the app
 *   when they sign in. The SDK stores it and never invents one.
 *
 * A note on backups, because it is a real and under-discussed failure: on both
 * platforms the default storage is included in device backups, so restoring a
 * backup onto a second device can carry an `anonymous_id` with it and make two
 * devices look like one. Where the platform allows it, the storage backing this
 * SDK should be excluded from backup — `NSURLIsExcludedFromBackupKey` on iOS,
 * `android:allowBackup="false"` or a backup rule on Android. The SDK cannot
 * enforce that from JavaScript, so it is documented here and in the README
 * rather than silently assumed.
 */
import { KEYS, type Storage } from "./storage";
import type { RandomSource } from "./uuid7";
import { uuid7 } from "./uuid7";

/**
 * A session ends after this long without activity. Thirty minutes is the
 * convention across analytics tools; matching it means numbers here can be
 * compared with numbers from elsewhere without a footnote.
 */
export const SESSION_TIMEOUT_MS = 30 * 60 * 1000;

interface StoredSession {
  id: string;
  lastSeen: number;
}

export class Identity {
  private anonymousId: string | null = null;
  private userId: string | null = null;
  private session: StoredSession | null = null;

  constructor(
    private readonly storage: Storage,
    private readonly random: RandomSource,
  ) {}

  /** @returns whether the anonymous id was minted now — the signal for a first
   *  launch, used by install detection. */
  async load(now: number = Date.now()): Promise<{ firstLaunch: boolean }> {
    this.userId = await this.storage.get(KEYS.userId);

    const existing = await this.storage.get(KEYS.anonymousId);
    if (existing) {
      this.anonymousId = existing;
    } else {
      this.anonymousId = uuid7(this.random, now);
      await this.storage.set(KEYS.anonymousId, this.anonymousId);
    }

    const rawSession = await this.storage.get(KEYS.session);
    if (rawSession) {
      try {
        this.session = JSON.parse(rawSession) as StoredSession;
      } catch {
        this.session = null;
      }
    }
    return { firstLaunch: !existing };
  }

  get deviceId(): string {
    if (!this.anonymousId) {
      throw new Error("mmp: identity used before initialize() completed");
    }
    return this.anonymousId;
  }

  get user(): string | null {
    return this.userId;
  }

  /**
   * A fresh id for one event.
   *
   * Lives here because this class owns the random source. Minted once at track
   * time and then never regenerated — the server deduplicates on it, so a new
   * id on a retry would turn one purchase into two.
   */
  mintEventId(now: number = Date.now()): string {
    return uuid7(this.random, now);
  }

  async setUserId(userId: string | null): Promise<void> {
    this.userId = userId;
    if (userId === null) {
      await this.storage.remove(KEYS.userId);
    } else {
      await this.storage.set(KEYS.userId, userId);
    }
  }

  /**
   * The current session id, starting a new one if the last activity is old
   * enough. Called on every event, so it also records the activity.
   */
  async touchSession(now: number = Date.now()): Promise<string> {
    const current = this.session;
    if (current && now - current.lastSeen < SESSION_TIMEOUT_MS) {
      this.session = { id: current.id, lastSeen: now };
    } else {
      this.session = { id: uuid7(this.random, now), lastSeen: now };
    }
    await this.storage.set(KEYS.session, JSON.stringify(this.session));
    return this.session.id;
  }

  /**
   * Forget this device and start again.
   *
   * The honest response to a user asking not to be tracked any more: a new
   * anonymous id cannot be joined to the old one, so nothing recorded before
   * this point can be attributed to what comes after. The server-side erasure
   * endpoint deletes the history; this severs the future from it.
   */
  async reset(now: number = Date.now()): Promise<void> {
    this.anonymousId = uuid7(this.random, now);
    this.userId = null;
    this.session = null;
    await this.storage.set(KEYS.anonymousId, this.anonymousId);
    await this.storage.remove(KEYS.userId);
    await this.storage.remove(KEYS.session);
  }
}
