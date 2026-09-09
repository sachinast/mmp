# 07 — API Specification

**Status:** describes the built system. Endpoint inventory generated from the
running service — **50 paths, 68 operations, 55 schemas** on 2026-09-09.

The complete reference lives in [`docs/api/`](../api/README.md), generated
rather than hand-maintained so it cannot drift. `openapi.json` there is the
machine-readable contract; point a client generator at it.

This document covers the design rules behind that surface.

## Two APIs, not one

**Tracker** (`:8001`) takes device traffic: events, clicks, S2S, SKAdNetwork
postbacks, deep link resolution. App-key or signature authenticated. Latency-gated.

**Management API** (`:8002`) is configuration and reporting. Session cookies with
CSRF, RBAC.

They are separate services because they have different risk profiles, different
scaling characteristics and different failure tolerances. The tracker holds the
narrowest database grants of anything in the platform.

## Authentication

| Caller | Mechanism |
|---|---|
| SDK in an app | `Authorization: Bearer <client key>` |
| Your backend | Client key **plus** HMAC canonical-request signature |
| Dashboard / ops | Session cookie + `x-csrf-token` on unsafe methods |
| Apple | ECDSA signature in the postback body — the only authentication |

API keys are stored as **HMAC-SHA256 under a KMS-held pepper**, so possession of
the database is not enough to verify one. The raw key is returned once at
creation and never retrievable.

S2S signing covers method, path, timestamp and a body digest, with a nonce
replay cache. Signing only the body would let a request be replayed against a
different path.

## Design rules

**Bounded by default.** Every range endpoint refuses an unbounded range — 90
days for reports, 31 for exports. An unbounded scan of a partitioned table is a
denial of service that looks like a report. Row caps are refusals, not
truncations: a silently short CSV is worse than a failed one, because the
recipient cannot tell.

**Idempotent where it matters.** Events carry a client-minted `event_id`
(UUIDv7) and are deduplicated on it. Delivery to postbacks and webhooks is
at-least-once, and receivers are told to be idempotent.

**Indistinguishable failures where distinguishability leaks.** Three examples:

- Another tenant's app id returns `404`, not `403` — otherwise the API confirms
  which ids exist.
- The deferred deep link handshake returns one identical response for an unknown
  device, an expired window, and a device with no destination — otherwise it is
  an oracle for whether an identifier installed an app.
- The SKAdNetwork endpoint answers identically for stored, duplicate and
  unknown-app — otherwise it enumerates customers' App Store ids to anyone.

**Validation at configuration time.** A malformed postback template, an
out-of-range conversion value, an https-less endpoint: rejected when saved, not
discovered as a delivery that never arrives.

**Secrets are write-only.** Integration credentials are envelope-encrypted and
only the *names* of supplied fields are ever returned.

## Versioning

Everything is under `/v1`. Additive changes ship in place; breaking changes need
a new version. See [API_VERSIONING.md](../API_VERSIONING.md).

## Errors

JSON with a `detail` string. `4xx` will never succeed as sent — do not retry.
`5xx` and `429` are worth retrying, with jittered backoff.

`429` carries `retry-after` in seconds. Only the numeric form is emitted;
parsing an HTTP-date to decide a sleep means trusting a device clock.

## Rate limiting

Per app on the ingest path, per app on deep link resolution — the latter on its
own budget, because it costs a database query and is the endpoint an attacker
would use to probe identifiers.

## What is not built

- **No public read API for third parties.** The management API assumes a session,
  which suits a dashboard and not a partner integration. A machine-to-machine
  read credential is unbuilt.
- **No GraphQL, no bulk mutation, no pagination cursors** on the smaller
  collection endpoints — they return complete lists with a hard cap.
- **No webhook replay-by-range**, only per-delivery retry.
