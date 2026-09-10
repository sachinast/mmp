# 14 — Integrations Specification

**Status:** living. The framework is built; no real ad network adapter ships.

## The adapter contract

`packages/mmp_providers/base.py` defines a `Protocol`. An adapter turns an event
plus configuration into a `PreparedRequest`, and reads a response into a
`DeliveryVerdict`.

It may **not**:

- send the request itself
- decide retries
- reach the database
- see values outside the template allowlist

That narrowness is the security model. A new adapter cannot introduce an SSRF
hole, a retry storm, or a cross-tenant read, because it is never handed the
means to. A test asserts adapters cannot even import an HTTP client.

Registration is **explicit**, not entry-point discovery: an installed package
should not be able to start receiving conversion data by virtue of being on the
path.

## Shipped adapters

Two, both generic:

- **`custom`** — URL template substitution.
- **`s2s_json`** — JSON POST with bearer auth and an event name map.

**Neither is named after a real network, deliberately.** The framework is what
was built; naming one bakes a partner's quirks into the core. A test asserts no
network name appears outside the adapter directory.

Adding a real network is a self-contained adapter plus its tests. Which networks
ship first is [an open product decision](01-prd.md#open-questions).

## Delivery

Postbacks and webhooks share the outbound path:

- **SSRF defence**: resolve, check against blocked ranges, pin the connection to
  that address, follow no redirects.
- **Allowlist template substitution** — never a template engine. SSTI becomes
  RCE.
- **At-least-once delivery** with jittered exponential backoff, then abandonment
  with the reason recorded.
- **Adapter verdicts are honoured**: a `200` carrying `{"error": …}` is a
  failure. That was a real bug — `interpret()` was decorative until a mutation
  test caught it.

## Event map semantics

A configured `event_map` **replaces** the adapter default rather than merging
into it. The map is a filter as well as a translation, and a filter you can only
add to is not a filter — with merge semantics, "only send installs" would be
inexpressible, which advertisers ask for constantly.

The cost is that renaming one event means listing them all. That is visible in a
configuration; silently sending an event someone tried to exclude is not.

## Credentials

Envelope-encrypted (AES-256-GCM, KMS-wrapped DEK, AAD bound to the
organisation). Only the **names** of supplied fields are ever returned.

Each adapter declares what it needs — field name, label, whether it is secret,
whether it is required — and the dashboard renders a form from that. The
declaration is what decides where a value goes: secret fields become encrypted
credentials, everything else is plain configuration. A test asserts every
declared-required field is one `validate()` actually rejects the absence of,
because two statements of the same requirement drift, and the way it fails is a
form that stops asking for something still mandatory. Validated
against the adapter at save time, returning all problems at once rather than one
per attempt.

## Webhooks

Your own endpoint rather than a network's. HMAC-signed; verify with
`compare_digest`, never `==`. At-least-once, so **make your receiver
idempotent** — a receiver that double-counts a retry will double-count under any
network partition.

## SKAdNetwork

A different kind of integration: Apple posts to us. See
[SKADNETWORK.md](../SKADNETWORK.md). Registering as an ad network with Apple and
signing ads are business processes this platform does not substitute for.

## Open

- No real network adapters.
- No OAuth flow for networks that require it.
- No adapter marketplace or third-party adapter loading — and given the
  explicit-registration decision above, that would need real thought.
- No automated health checking of configured integrations beyond
  `last_health_check_at`, which nothing currently writes.
