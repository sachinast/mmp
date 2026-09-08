# MMP — Mobile Measurement Platform

Event tracking, campaign attribution, and conversion distribution for mobile
apps. Python service tier, Postgres system of record.

The architecture, latency budgets, correctness invariants and phase plan live in
[`mmp-python-plan.html`](mmp-python-plan.html) (also published as an Artifact). This file is how to run it.

## Requirements

- Python 3.12+
- Postgres 16 and Redis 7 — either locally (`brew services start postgresql@16 redis`)
  or in containers (`make infra-up`)
- [uv](https://docs.astral.sh/uv/) (`brew install uv`)

## Setup

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"   # once per pepper
make setup
make db-create
make migrate
make dev-roles      # lets tests connect as the application roles, so RLS is
                    # actually exercised rather than bypassed by the owner
```

Settings are validated at boot. A missing or placeholder secret is a startup
failure, not a runtime surprise.

## Running

```bash
make tracker    # :8001  event ingestion and click redirects
make api        # :8002  auth, orgs, apps, campaigns, analytics
make web        # :8003  dashboard
make worker     #        background processing

make dev-stack  # API + dashboard together, for browsing at :8003
```

Every HTTP service answers `/health` (is the process alive) and `/ready` (should
it receive traffic — runs dependency probes, reports draining during shutdown).

## Quality gates

```bash
make check     # lint + types + security + tests, exactly what CI runs
make fmt       # autofix
make audit     # dependency CVE scan against the lockfile
make bench     # ingest latency regression gate
```

`make bench` compares median latency against `infra/load/baseline.json`. Only
medians are gated — on a developer machine the tails are dominated by GC pauses
and contention with the load generator, and a gate that fires on noise is one
people learn to ignore. It is a **regression gate, not an SLO check** — it runs a Python load generator against
a Python server on the same cores and is itself the bottleneck. Capacity and the
published p99 < 120 ms SLO are measured with `infra/load/ingest.k6.js` against
deployed infrastructure.

## Credentials

| Secret | At rest | Why |
| --- | --- | --- |
| User password | argon2id | Verified once per login, behind a rate limiter. Slow is the feature. |
| API key | HMAC-SHA256 under a KMS pepper | Verified on **every ingest request**. A slow hash here would be a self-inflicted denial of service; 256 bits of entropy does the work instead. |
| Partner credentials | AES-256-GCM, KMS-wrapped DEK | Must be recoverable. Bound to the owning organisation, so a row copied to another tenant fails to decrypt. |
| Session | Opaque token in Redis | Revocable. A JWT would make logout a lie. |

## How a click becomes a redirect

```
GET /c/{code}  ->  dict lookup in the in-process link cache   (no database)
               ->  mint a UUIDv7 click_id
               ->  classify the user agent by substring       (no UA library)
               ->  hash the IP; the raw address is discarded
               ->  append to the click buffer                 (no await on Redis)
               ->  302 to the store
```

Measured at **p50 2.4 ms** over loopback. Nothing in that path can block: the
person on the other end is waiting to reach an app store, and every hundred
milliseconds is a share of them who leave instead.

The link cache is kept fresh by Postgres `LISTEN`/`NOTIFY` — so "I disabled that
link" takes effect in milliseconds — with a five-minute full resync as the
backstop, because notifications are fire-and-forget and a process that was
disconnected never hears them.

On Android the click id travels inside the Play Store `referrer` parameter and
comes back through the Install Referrer API on first launch. That is what makes
Android attribution deterministic. iOS has no equivalent channel, which is why
it needs SKAdNetwork rather than a referrer — not an omission to fix later.

## Sending data out

The postback engine fetches **user-supplied URLs from our servers**, which is
the sharpest attack surface here. Unguarded, anyone who can create a rule has a
request forwarder inside the VPC — pointed, above all, at `169.254.169.254`,
which on a misconfigured instance hands out credentials.

`mmp_core.outbound` closes that: https only, resolve the hostname ourselves,
reject every returned address in a private/loopback/link-local/metadata range,
then **connect to the validated IP** with the original `Host` and SNI. That last
step is what defeats DNS rebinding — a hostname allowlist alone does not,
because the attacker only has to make the client's own second lookup answer
differently. Redirects are refused outright; a 302 to an internal address would
walk past every check above.

Postback templates are **not** rendered by a template engine. Jinja is a
programming language, and rendering customer-supplied templates with it hands
anyone who can create a rule server-side template injection and, from there,
code execution on a worker holding database credentials. `mmp_providers.templates`
is a fixed allowlist of variable names substituted from a dict, with every value
URL-encoded. A `{{...}}` that is not a well-formed, allowlisted placeholder is
rejected when the rule is saved.

Delivery is **claimed before it is attempted** — `UNIQUE (postback_rule_id,
event_id)` decides the winner, and every other worker walks away. A duplicate
postback inflates a campaign's apparent performance and, on a cost-per-action
deal, means paying twice for one action.

S2S conversions are signed over a canonical request (method, path, timestamp,
body digest) with a nonce cache behind it: a captured request replayed is a
duplicate conversion sent to an ad network, and a bearer token alone does not
stop that.

Postback rules and webhooks are configured through `/v1/postback-rules` and
`/v1/webhooks`. Two rules govern both: a bad rule **fails when it is saved**, not
when it is delivered — an unknown template variable or a destination we refuse
to reach is a 422 while the user is looking at the form; and credentials are
**write-only** — a postback's header values and a webhook's signing secret are
encrypted at rest and never returned, the secret shown exactly once at creation
alongside the verification code a customer needs to check it.

Webhooks auto-disable after 20 consecutive failures. That is a courtesy to the
receiver: an endpoint returning 500s for a day is not recovering on its own, and
retrying into it generates load on a broken system. Any success resets the
count, so intermittent trouble never accumulates into a disable.

## The dashboard

Server-rendered Jinja, and deliberately thin: it holds no database connection,
no business logic and no service credentials. Every page is the result of
calling the API **as the signed-in user**, forwarding their own session cookie,
so it cannot surface anything that user could not fetch themselves. A template
bug cannot become a tenancy bug, because the tenancy decision was never made
there.

Everything it loads is inline — no CDN, no external stylesheet, no bundler —
which is why its Content-Security-Policy can be `default-src 'none'`. Charts are
server-rendered SVG. A dashboard that loads third-party JavaScript is one
compromised CDN away from exfiltrating tenant data.

## Why the dashboard never queries raw events

On Postgres, rollups are not an optimisation — they are the read path. A
dashboard querying raw events is fine at ten million rows and unusable at a
billion, and nobody notices the transition until an advertiser does.

| Rollup | Grain | Cost |
| --- | --- | --- |
| `rollup_events_hourly` | event × platform, hourly | cheap |
| `rollup_clicks_hourly` | campaign × platform, hourly | cheap |
| `rollup_campaign_daily` | campaign, daily | pays for the events↔attributions join — the query to watch as volume grows |

Buckets are keyed on **`occurred_at`** and scanned by **`received_at`**. That
distinction is the subtlest thing in the design: an advertiser asking for
"installs on Tuesday" means installs that *happened* on Tuesday, so a device
back from a week offline must not appear as a spike today. But the tables are
partitioned on arrival, which is what bounds a scan. So each refresh scans an
arrival window, groups by occurrence, and writes only buckets its window covers
completely — older buckets are left to the nightly late-arrival pass, whose
window is wide enough to recompute them in full.

Every refresh **recomputes** rather than increments, so running it twice is a
no-op. The trailing and late-arrival passes cover overlapping windows by design.

Analytics endpoints enforce three things on every request: a **required** date
range (a default range is a default scan), a 90-day cap, and a Redis cache keyed
by organisation so a hit cannot cross a tenant.

## How an install becomes an attribution

Deterministic last-click, in a strict precedence order. There is no
probabilistic tier: if nothing deterministic matches, the install is organic.

| | Signal | Why it ranks here |
| --- | --- | --- |
| 1 | **Referrer** | A click id inside the Play Install Referrer. Ground truth on Android — it comes from Google, survives the install, and cannot be claimed by a competing network. |
| 2 | **Click ID** | The SDK was handed one through a deferred deep link. Trustworthy, but it passed through the device. |
| 3 | **Device match** | The same hashed advertising ID at click and at install. Deterministic, but only when the network passed it and the user has not opted out. |
| 4 | **Organic** | Nothing matched. Not a failure — the honest answer. |

Ties break to the most recent qualifying click. Better evidence arriving late
(Play's API is queried on first launch and may need a retry) **supersedes**
rather than overwrites: the old row is kept and pointed at its replacement, so a
number already reported to an ad network stays reconstructable.

`packages/mmp_attrib/` is pure — `now` and every input are parameters, nothing
does IO — which is what makes `tests/test_attribution_engine.py` possible: 31
golden cases with exact expected outcomes, running in 20 ms. Any future change
to attribution has to declare itself by breaking one of those rather than by
quietly moving a customer's numbers.

Two things the engine deliberately does **not** do:

- **No fingerprinting.** IP-plus-device-model matching would raise the match
  rate, is what Apple's rules prohibit for cross-app attribution, and produces
  attributions that cannot be defended when an advertiser asks how a number was
  derived.
- **No blocking on suspected fraud.** An implausibly short click-to-install gap
  (click injection) is recorded and flagged, and the install is still
  attributed. Refusing would penalise the advertiser for their attacker.

## How an event becomes a row

```
POST /v1/events  ->  authenticate (Redis-cached key record, HMAC compare)
                 ->  size + decompression caps, msgspec validation
                 ->  stamp received_at at the edge
                 ->  Redis idempotency window (catches SDK retries)
                 ->  append to a bounded in-process buffer, return 202
                        |
                        v  background, every 50 ms or 500 events
                 ->  Redis Stream (consumer group, replayable)
                        |
                        v  worker
                 ->  COPY into a TEMP staging table
                 ->  INSERT ... SELECT ... ON CONFLICT DO NOTHING
                 ->  acknowledge  <- only now
```

Two deduplication layers, because there are two different failures:

| Failure | Caught by | Guarantee |
| --- | --- | --- |
| Stream redelivery (worker crashed before ack) | Primary key `(received_at, app_id, event_id)` — `received_at` is stamped at the edge and carried in the message, so a redelivery reproduces the exact key | Exact |
| SDK retry after a lost 202 (new request, new `received_at`) | Redis idempotency window on `(app_id, event_id)` | Best effort over a bounded window; residual measured by reconciliation |

## Data model

Three classes of table, deliberately treated differently:

| | Tables | Written by | Read by |
| --- | --- | --- | --- |
| **Business** | 18, ORM-mapped, RLS-enforced | the API | the API |
| **Events** | `clicks`, `events` — day-partitioned, no ORM | `COPY` batches | rollup workers |
| **Derived** | rollups, usage, audit | workers | the dashboard |

Event tables are partitioned by day. Retention is `DROP TABLE` on an expired
partition — deleting a day of events row by row would hand autovacuum a fight it
cannot win on a table that is simultaneously absorbing inserts.

## Layout

```
services/
  tracker/   Starlette. The hot path. No ORM, no synchronous DB write.
  api/       FastAPI + SQLAlchemy. Business logic.
  worker/    Stream consumers and arq jobs.
  web/       Jinja + HTMX dashboard.
packages/
  mmp_core/      settings, logging, IDs, ASGI middleware, health, lifecycle
  mmp_crypto/    key hashing, HMAC, envelope encryption, IP hashing
  mmp_db/        SQLAlchemy models, migrations, RLS policies
  mmp_ingest/    stream producer/consumer, COPY batch writer
  mmp_attrib/    attribution engine — pure functions, no IO
  mmp_providers/ partner adapters behind one Protocol
```

## Rules that are enforced, not remembered

These are tests, not conventions. They fail the build:

- **No f-string SQL.** asyncpg is parameterised-only (`tests/test_no_fstring_sql.py`).
- **No pickle.** Queue payloads are JSON or msgpack; pickle is a deserialisation
  RCE and it is why Celery is not in this stack.
- **No PII in logs.** A redaction processor masks sensitive keys and a test
  asserts the key list stays complete.
- **No placeholder secrets.** Settings reject values starting with `change`,
  `placeholder`, `todo`.
- **No live metadata in migrations.** A migration must keep doing what it did on
  the day it was written; importing a list that grows makes an applied migration
  change behaviour later. A test walks every migration's imports, and a slow test
  applies the whole chain to a fresh database — the only check that catches a
  chain which works incrementally and fails from empty.
- **No tenant table without an RLS policy.** A test walks the ORM metadata and
  fails if a model carrying `OrgScopedMixin` has no `org_isolation` policy.
- **No `SET` where `SET LOCAL` belongs.** Tenancy set with session scope would
  leak to the next request on a pooled connection; a test asserts the
  `is_local` argument.
- **Only three modules may build SQL from an identifier.** Everywhere else, an
  f-string containing SQL fails the build. The three are in `mmp_db`, and each
  validates identifiers against an allowlist.
- **The raw API key leaves the system once.** Tests assert it is absent from
  listings, from the database row, from logs, and from the object's own `repr`.

## Progress

- [x] **Phase 0** — foundation, observability, quality gates, versioning policy
- [x] **Phase 1** — schema, partitioning, RLS and tenancy
- [x] **Phase 2** — auth, organisations, apps, API keys
- [x] **Phase 3** — ingest pipeline end to end
- [x] **Phase 4** — campaigns, links, click tracking
- [x] **Phase 5** — attribution and Play Install Referrer
- [x] **Phase 6** — sessions, rollups, analytics API
- [x] **Phase 7** — dashboard
- [ ] Phase 8 — React Native SDK
- [x] **Phase 9** — S2S, postbacks, webhooks
- [ ] Phase 10 — reliability and security audit
- [ ] Phase 11 — privacy, consent, provider framework
- [ ] Phase 12 — deep links, fraud signals, export
- [ ] Phase 13 — iOS attribution (parallel workstream)
