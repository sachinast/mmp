/**
 * HTTP, and the classification of what came back.
 *
 * The important part is not the request, it is deciding what a failure means.
 * Three outcomes, and conflating any two of them loses data or duplicates it:
 *
 * - `accepted` — the server has it. Drop the events; sending again would be a
 *   duplicate the server has to deduplicate for us.
 * - `retry` — nobody knows yet. Network failures, timeouts, 5xx, 429. Keep the
 *   events and try later. Some of these *did* reach the server, which is
 *   exactly why every event carries a client-minted `event_id`: the retry is
 *   deduplicated server-side rather than becoming a double-counted purchase.
 * - `rejected` — the server understood and refused. A malformed batch or a
 *   revoked key. Retrying is pointless and an infinite loop against someone
 *   else's servers, so the events are dropped and the failure is reported.
 */
import type { Logger, WireEvent } from "./types";

export type SendOutcome =
  | { kind: "accepted" }
  | { kind: "retry"; reason: string; retryAfterMs?: number }
  | { kind: "rejected"; reason: string; status: number };

export interface TransportOptions {
  endpoint: string;
  apiKey: string;
  timeoutMs?: number;
  logger?: Logger;
  fetchImpl?: typeof fetch;
}

const DEFAULT_TIMEOUT_MS = 15_000;

export class Transport {
  private readonly fetchImpl: typeof fetch;

  constructor(private readonly options: TransportOptions) {
    const impl = options.fetchImpl ?? globalThis.fetch;
    if (typeof impl !== "function") {
      throw new Error("no fetch implementation available");
    }
    this.fetchImpl = impl;
  }

  async send(events: WireEvent[]): Promise<SendOutcome> {
    return this.post("/v1/events", { events });
  }

  /**
   * The deferred deep link handshake. Returns the destination, or null — and
   * treats a failure as "no destination" rather than surfacing it, because the
   * app is deciding which screen to open and blocking that on our availability
   * would make a first launch hang on our uptime.
   */
  async resolveDeferredDeepLink(anonymousId: string): Promise<string | null> {
    try {
      const response = await this.request("/v1/deeplink/resolve", {
        anonymous_id: anonymousId,
      });
      if (!response.ok) return null;
      const body = (await response.json()) as { destination?: string | null };
      return body.destination ?? null;
    } catch {
      return null;
    }
  }

  private async post(path: string, body: unknown): Promise<SendOutcome> {
    let response: Response;
    try {
      response = await this.request(path, body);
    } catch (error) {
      // Includes the timeout abort. Indistinguishable from a network failure
      // on purpose: in both cases the request may or may not have arrived.
      return { kind: "retry", reason: describe(error) };
    }

    if (response.status >= 200 && response.status < 300) {
      return { kind: "accepted" };
    }

    if (response.status === 429 || response.status === 408) {
      return {
        kind: "retry",
        reason: `http ${response.status}`,
        ...parseRetryAfter(response.headers.get("retry-after")),
      };
    }

    if (response.status >= 500) {
      return { kind: "retry", reason: `http ${response.status}` };
    }

    // 4xx. The server understood and said no; sending it again says the same.
    return {
      kind: "rejected",
      reason: `http ${response.status}`,
      status: response.status,
    };
  }

  private async request(path: string, body: unknown): Promise<Response> {
    const controller = new AbortController();
    const timeout = setTimeout(
      () => controller.abort(),
      this.options.timeoutMs ?? DEFAULT_TIMEOUT_MS,
    );
    try {
      return await this.fetchImpl(`${trimEnd(this.options.endpoint)}${path}`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          authorization: `Bearer ${this.options.apiKey}`,
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timeout);
    }
  }
}

function trimEnd(url: string): string {
  return url.endsWith("/") ? url.slice(0, -1) : url;
}

function parseRetryAfter(header: string | null): { retryAfterMs?: number } {
  if (!header) return {};
  const seconds = Number(header);
  // Only the numeric form. The HTTP-date form is also legal, but parsing a
  // date from a header to decide a sleep means trusting the device clock,
  // which on a phone can be wrong by hours.
  if (!Number.isFinite(seconds) || seconds < 0) return {};
  return { retryAfterMs: Math.min(seconds * 1000, 300_000) };
}

function describe(error: unknown): string {
  if (error instanceof Error) return error.name === "AbortError" ? "timeout" : error.name;
  return "network error";
}
