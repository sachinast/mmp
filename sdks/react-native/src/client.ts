/**
 * The SDK proper: what an app actually calls.
 *
 * Three rules shape everything here.
 *
 * **Never throw into the host app.** A measurement SDK that crashes someone's
 * checkout has done far more damage than it could ever be worth. Every public
 * method catches, reports through the logger, and returns. The one exception is
 * `initialize`, which validates its configuration loudly — that is a
 * programming error, it happens on the developer's machine, and failing
 * silently there means shipping an app that measures nothing.
 *
 * **Never block.** `track` persists and returns; sending happens on a timer.
 * The app's frame budget is not ours to spend.
 *
 * **Never read an identifier we have not been permitted to read.** The
 * advertising ID is requested from the platform only when the `advertising`
 * purpose is granted, so a denial means the value is never obtained rather
 * than obtained and then withheld.
 */
import { Consent } from "./consent";
import { ConversionValues } from "./conversion";
import { Identity } from "./identity";
import { NO_NATIVE, callNative, type DeviceInfo, type NativeBridge } from "./native";
import { EventQueue } from "./queue";
import { KEYS, MemoryStorage, SafeStorage, type Storage } from "./storage";
import { Transport } from "./transport";
import {
  LIMITS,
  RESERVED_EVENTS,
  canonicalEventName,
  type ConsentUpdate,
  type EventProperties,
  type Logger,
  type MmpConfig,
  type TrackOptions,
  type WireEvent,
} from "./types";
import { platformRandom, type RandomSource } from "./uuid7";

const DEFAULTS = {
  batchSize: 20,
  flushIntervalMs: 10_000,
  maxQueueSize: 1_000,
};

export interface ClientDependencies {
  storage?: Storage;
  native?: NativeBridge;
  random?: RandomSource;
  fetchImpl?: typeof fetch;
  now?: () => number;
}

const silentLogger: Logger = {
  debug() {},
  warn() {},
  error() {},
};

export class MmpClient {
  private readonly logger: Logger;
  private readonly storage: Storage;
  private readonly native: NativeBridge;
  private readonly identity: Identity;
  private readonly consent: Consent;
  private readonly conversions: ConversionValues;
  private readonly queue: EventQueue;
  private readonly transport: Transport;
  private readonly now: () => number;
  private readonly config: Required<Pick<MmpConfig, "batchSize" | "flushIntervalMs" | "maxQueueSize">>;
  private readonly trackWithoutConsent: boolean;

  private timer: ReturnType<typeof setInterval> | null = null;
  private started = false;
  private device: DeviceInfo = {};
  private deferredDeepLink: string | null = null;

  constructor(config: MmpConfig, deps: ClientDependencies = {}) {
    validate(config);

    this.logger = config.logger ?? (config.debug ? consoleLogger() : silentLogger);
    this.now = deps.now ?? (() => Date.now());
    this.native = deps.native ?? NO_NATIVE;

    if (!deps.storage) {
      // Loud, because the consequence is not obvious: with no persistence every
      // launch mints a new anonymous id, so every launch looks like a new
      // install. That inflates the number a customer is billed on and destroys
      // attribution, and it would otherwise look like the SDK working fine.
      this.logger.warn(
        "mmp: no storage supplied — falling back to memory. Installs will be " +
          "counted repeatedly and attribution will not work. Pass an " +
          "AsyncStorage-backed adapter.",
      );
    }
    this.storage = new SafeStorage(deps.storage ?? new MemoryStorage(), (operation, error) =>
      this.logger.warn("mmp: storage failed", { operation, error: String(error) }),
    );

    this.config = {
      batchSize: Math.min(config.batchSize ?? DEFAULTS.batchSize, LIMITS.eventsPerBatch),
      flushIntervalMs: config.flushIntervalMs ?? DEFAULTS.flushIntervalMs,
      maxQueueSize: config.maxQueueSize ?? DEFAULTS.maxQueueSize,
    };
    this.trackWithoutConsent = config.trackWithoutConsent ?? true;

    const random = deps.random ?? platformRandom();
    this.identity = new Identity(this.storage, random);
    this.consent = new Consent(this.storage);
    this.conversions = new ConversionValues(this.storage);
    this.transport = new Transport({
      endpoint: config.endpoint,
      apiKey: config.apiKey,
      logger: this.logger,
      ...(deps.fetchImpl ? { fetchImpl: deps.fetchImpl } : {}),
    });
    this.queue = new EventQueue({
      storage: this.storage,
      transport: this.transport,
      batchSize: this.config.batchSize,
      maxQueueSize: this.config.maxQueueSize,
      logger: this.logger,
    });
  }

  async initialize(): Promise<void> {
    if (this.started) return;
    this.started = true;
    try {
      await this.consent.load();
      const { firstLaunch } = await this.identity.load(this.now());
      this.device = await callNative(this.native.getDeviceInfo?.bind(this.native), {});
      await this.queue.load();

      await this.reportInstallOnce(firstLaunch);
      this.startTimer();
      void this.flush();
      // Not awaited. The mapping is only useful on iOS and only once an event
      // happens, so a first launch should not wait on a network call for it.
      void this.loadConversionMappings();
    } catch (error) {
      this.logger.error("mmp: initialize failed", { error: String(error) });
    }
  }

  /**
   * The install event, sent exactly once per install.
   *
   * Guarded by a persisted flag rather than by "is this the first launch",
   * because those differ: an install whose event failed to send must still be
   * reported on the next launch, and a device that was merely cleared of its
   * queue must not be reported twice.
   */
  private async reportInstallOnce(firstLaunch: boolean): Promise<void> {
    const reported = await this.storage.get(KEYS.installReported);
    if (reported) return;
    if (!firstLaunch) {
      // An existing device with no install flag: the SDK was added to an app
      // that already had users. Recording them as new installs would invent a
      // spike of installs that never happened, so the flag is set without
      // sending anything.
      await this.storage.set(KEYS.installReported, "existing");
      return;
    }

    const referrer = await callNative(this.native.getInstallReferrer?.bind(this.native), null);
    const properties: EventProperties = {};
    if (referrer) properties["install_referrer"] = referrer;
    const advertisingId = await this.advertisingId();
    if (advertisingId) properties["advertising_id"] = advertisingId;

    await this.enqueue("install", { properties });
    await this.storage.set(KEYS.installReported, "sent");
  }

  /**
   * Requested from the platform only when the purpose is granted. A denial
   * means the identifier is never read — which is a stronger guarantee than
   * reading it and choosing not to send it.
   */
  private async advertisingId(): Promise<string | null> {
    if (!this.consent.allows("advertising") && !this.consent.allows("attribution")) return null;
    return callNative(this.native.getAdvertisingId?.bind(this.native), null);
  }

  async track(eventName: string, options: TrackOptions = {}): Promise<void> {
    // Compared canonically, matching the server. Otherwise `track("Install")`
    // is allowed through here and then handled as an ordinary event, which is
    // the silent-failure shape this whole check exists to prevent.
    const reserved = (RESERVED_EVENTS as readonly string[]).map(canonicalEventName);
    if (reserved.includes(canonicalEventName(eventName))) {
      this.logger.error(
        "mmp: that event name is reserved — the SDK sends it for you",
        { eventName },
      );
      return;
    }
    await this.enqueue(eventName, options);
    await this.reportConversion(eventName);
  }

  private async loadConversionMappings(): Promise<void> {
    if (this.device.platform !== "ios") return;
    try {
      const mappings = await this.transport.fetchConversionValues();
      if (mappings) this.conversions.setMappings(mappings);
    } catch (error) {
      this.logger.debug("mmp: conversion mapping unavailable", { error: String(error) });
    }
  }

  /**
   * Tells Apple about this event, if it would move the value.
   *
   * The decision is made in `ConversionValues` rather than here, and it matters
   * that it is made at all: Apple ignores a decrease instead of reporting one,
   * and every accepted call restarts the measurement window — so an SDK that
   * called on every event would look like it was working while discarding most
   * of what it sent, and would delay its own postback doing it.
   */
  private async reportConversion(eventName: string): Promise<void> {
    if (this.device.platform !== "ios") return;
    try {
      const update = await this.conversions.apply(eventName);
      if (!update) return;
      await callNative(
        this.native.updateConversionValue
          ? () =>
              this.native.updateConversionValue!(update.fineValue, update.coarseValue)
          : undefined,
        false,
      );
    } catch (error) {
      this.logger.debug("mmp: conversion value not reported", { error: String(error) });
    }
  }

  async setUserId(userId: string | null): Promise<void> {
    try {
      const previous = this.identity.user;
      await this.identity.setUserId(userId);
      // `login` is what links a device to a person server-side. Sent only on a
      // change, so a signed-in app relaunching does not re-link every time.
      if (userId && userId !== previous) await this.enqueue("login", {});
    } catch (error) {
      this.logger.error("mmp: setUserId failed", { error: String(error) });
    }
  }

  async setConsent(update: ConsentUpdate): Promise<void> {
    try {
      const changed = await this.consent.update(update);
      if (Object.keys(changed).length === 0) return;
      await this.enqueue("consent_update", { properties: { ...changed } });
      // Flushed immediately: a consent decision governs how everything already
      // queued may be processed, and the server applies consent events in a
      // batch before the events that follow them.
      void this.flush();
    } catch (error) {
      this.logger.error("mmp: setConsent failed", { error: String(error) });
    }
  }

  /**
   * Where this install was originally headed, if it came from a deep link.
   *
   * Returns null rather than waiting when the answer is not known — the app is
   * choosing which screen to show, and blocking that on a network call would
   * make a first launch hang on our availability.
   */
  async getDeferredDeepLink(): Promise<string | null> {
    if (this.deferredDeepLink) return this.deferredDeepLink;
    try {
      const cached = await this.storage.get(KEYS.deepLink);
      if (cached) {
        this.deferredDeepLink = cached;
        return cached;
      }
      const resolved = await this.transport.resolveDeferredDeepLink(this.identity.deviceId);
      if (resolved) {
        this.deferredDeepLink = resolved;
        await this.storage.set(KEYS.deepLink, resolved);
      }
      return resolved;
    } catch (error) {
      this.logger.debug("mmp: deferred deep link unavailable", { error: String(error) });
      return null;
    }
  }

  async flush(): Promise<void> {
    try {
      await this.queue.flush(this.now());
    } catch (error) {
      this.logger.error("mmp: flush failed", { error: String(error) });
    }
  }

  /** Severs future data from everything recorded before now. Server-side
   *  deletion is a separate request to the privacy API. */
  async reset(): Promise<void> {
    try {
      await this.identity.reset(this.now());
      await this.conversions.clear();
      this.deferredDeepLink = null;
      await this.storage.remove(KEYS.deepLink);
    } catch (error) {
      this.logger.error("mmp: reset failed", { error: String(error) });
    }
  }

  async shutdown(): Promise<void> {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    await this.flush();
  }

  get stats() {
    return this.queue.stats;
  }

  private startTimer(): void {
    if (this.timer) return;
    this.timer = setInterval(() => void this.flush(), this.config.flushIntervalMs);
    // Node keeps the process alive for a pending timer; React Native does not
    // care, but a test suite would hang without this.
    (this.timer as { unref?: () => void }).unref?.();
  }

  private async enqueue(eventName: string, options: TrackOptions): Promise<void> {
    try {
      if (!this.started) {
        this.logger.error("mmp: track() called before initialize()", { eventName });
        return;
      }
      if (!this.trackWithoutConsent && !this.consent.known && eventName !== "consent_update") {
        this.logger.debug("mmp: holding event until consent is known", { eventName });
        return;
      }

      const problem = validateEvent(eventName, options);
      if (problem) {
        // Refused here rather than by the server, so the developer sees it on
        // their machine instead of as a 422 in production.
        this.logger.error("mmp: event refused", { eventName, problem });
        return;
      }

      const now = this.now();
      const event: WireEvent = {
        event_id: this.identity.mintEventId(now),
        event_name: eventName,
        anonymous_id: this.identity.deviceId,
        occurred_at: (options.occurredAt ?? new Date(now)).toISOString(),
        session_id: await this.identity.touchSession(now),
        ...(this.identity.user ? { user_id: this.identity.user } : {}),
        ...(this.device.platform ? { platform: this.device.platform } : {}),
        ...(this.device.osVersion ? { os_version: this.device.osVersion } : {}),
        ...(this.device.appVersion ? { app_version: this.device.appVersion } : {}),
        ...(this.device.deviceModel ? { device_model: this.device.deviceModel } : {}),
        ...(options.revenueMinor !== undefined ? { revenue_minor: options.revenueMinor } : {}),
        ...(options.currency ? { currency: options.currency } : {}),
        ...(options.properties ? { properties: options.properties } : {}),
      };
      await this.queue.enqueue(event);
    } catch (error) {
      this.logger.error("mmp: could not record event", { eventName, error: String(error) });
    }
  }
}

function consoleLogger(): Logger {
  /* eslint-disable no-console */
  return {
    debug: (m, c) => console.debug(m, c ?? ""),
    warn: (m, c) => console.warn(m, c ?? ""),
    error: (m, c) => console.error(m, c ?? ""),
  };
}

function validate(config: MmpConfig): void {
  if (!config.apiKey) throw new Error("mmp: apiKey is required");
  if (!config.endpoint) throw new Error("mmp: endpoint is required");
  if (!/^https?:\/\//.test(config.endpoint)) {
    throw new Error("mmp: endpoint must be an absolute http(s) URL");
  }
  if (config.endpoint.startsWith("http://") && !isLocal(config.endpoint)) {
    // Refused rather than warned. Over plain HTTP the API key is readable by
    // anyone on the network, and an SDK that permits it in production is how a
    // key ends up posted from someone else's script.
    throw new Error("mmp: endpoint must use https outside local development");
  }
}

function isLocal(endpoint: string): boolean {
  return /^http:\/\/(localhost|127\.0\.0\.1|10\.0\.2\.2)(:|\/|$)/.test(endpoint);
}

function validateEvent(eventName: string, options: TrackOptions): string | null {
  if (!eventName || eventName.length > LIMITS.eventName) {
    return `event_name must be 1-${LIMITS.eventName} characters`;
  }
  if (options.revenueMinor !== undefined) {
    if (!Number.isInteger(options.revenueMinor)) {
      // Money as a float is how a revenue report stops reconciling; the server
      // takes an integer of minor units and so does this.
      return "revenueMinor must be an integer number of minor units";
    }
    if (!options.currency) return "currency is required whenever revenueMinor is set";
  }
  if (options.properties) {
    let encoded: string;
    try {
      encoded = JSON.stringify(options.properties);
    } catch {
      return "properties must be JSON-serialisable";
    }
    if (encoded.length > LIMITS.propertiesBytes) {
      return `properties exceed ${LIMITS.propertiesBytes} bytes`;
    }
  }
  return null;
}
