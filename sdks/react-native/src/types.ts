/** Public types. These mirror the server's ingest contract exactly. */

/** Mirrors mmp_ingest.consent.Purpose. */
export type Purpose = "analytics" | "attribution" | "advertising";

/** Mirrors mmp_ingest.consent.State. */
export type ConsentState = "granted" | "denied";

export type ConsentUpdate = Partial<Record<Purpose, ConsentState>>;

/** Limits copied from mmp_ingest.schema, so the SDK refuses what the server
 *  would refuse — locally, where the developer sees it, rather than as a 422
 *  in production. */
export const LIMITS = {
  eventName: 120,
  id: 255,
  propertiesBytes: 16 * 1024,
  eventsPerBatch: 100,
} as const;

/**
 * Reserved on the server. `install` drives attribution, `login`/`signup` drive
 * identity resolution, and `consent_update` carries a consent decision — the
 * SDK sends all four itself, so an app sending one by hand would corrupt state
 * it does not own.
 */
export const RESERVED_EVENTS = ["install", "login", "signup", "consent_update"] as const;

/**
 * Fold an event name to the form reserved names are matched on — lowercase,
 * separators removed. Mirrors `canonical_event_name` on the server, and the
 * contract test asserts the two agree.
 *
 * The name is only folded for *matching*. What gets sent is what you passed.
 */
export function canonicalEventName(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]/g, "");
}

export interface EventProperties {
  [key: string]: unknown;
}

export interface TrackOptions {
  properties?: EventProperties;
  /** Minor units — cents, pence, paise. Integer, because floating point money
   *  is how a revenue report stops reconciling. */
  revenueMinor?: number;
  /** ISO 4217, required whenever revenueMinor is set. */
  currency?: string;
  /** Defaults to now. Set it when replaying something that happened offline. */
  occurredAt?: Date;
}

export interface MmpConfig {
  /** The app's public ingest key. This ships inside the binary and is not a
   *  secret — it authorises writing events for one app, nothing else. */
  apiKey: string;
  /** e.g. https://track.example.com — must be https outside development. */
  endpoint: string;
  /** How many events to send per request. Capped at LIMITS.eventsPerBatch. */
  batchSize?: number;
  /** How long to wait before flushing a partial batch. */
  flushIntervalMs?: number;
  /** Events kept when offline. Oldest are dropped first once full. */
  maxQueueSize?: number;
  /** Emits SDK diagnostics. Never receives event properties or identifiers. */
  logger?: Logger;
  /** Set false to hold every event until consent is granted. */
  trackWithoutConsent?: boolean;
  debug?: boolean;
}

export interface Logger {
  debug(message: string, context?: Record<string, unknown>): void;
  warn(message: string, context?: Record<string, unknown>): void;
  error(message: string, context?: Record<string, unknown>): void;
}

/** One event as it goes on the wire. Mirrors mmp_ingest.schema.IncomingEvent. */
export interface WireEvent {
  event_id: string;
  event_name: string;
  anonymous_id: string;
  occurred_at: string;
  user_id?: string;
  session_id?: string;
  platform?: string;
  os_version?: string;
  app_version?: string;
  device_model?: string;
  revenue_minor?: number;
  currency?: string;
  click_id?: string;
  properties?: EventProperties;
}
