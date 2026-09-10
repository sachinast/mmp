# 05 — System Architecture

**Status:** describes the built system.

## Shape

Four services over one PostgreSQL and one Redis, in a `uv` workspace monorepo.

```
                    ┌──────────────┐
   devices ────────▶│   tracker    │  Starlette · the hot path
   (SDK, clicks)    │  :8001       │  redirect, ingest, S2S, SKAN, deep links
                    └──────┬───────┘
                           │ Redis Streams
                    ┌──────▼───────┐
                    │    worker    │  arq · consumers + scheduled jobs
                    │              │  attribution, rollups, postbacks,
                    └──────┬───────┘  webhooks, fraud sweep, partitions
                           │
                    ┌──────▼───────┐        ┌──────────────┐
                    │  PostgreSQL  │◀───────│     api      │  FastAPI · :8002
                    │      16      │        │              │  management + reporting
                    └──────────────┘        └──────┬───────┘
                                                   │
                                            ┌──────▼───────┐
                                            │     web      │  dashboard
                                            └──────────────┘
```

**Packages:** `mmp_core` (settings, logging, metrics, ids, SSRF, deep link
validation), `mmp_db` (schema, migrations, pool, audit, erasure), `mmp_crypto`
(envelope encryption, KMS, hashing, signing), `mmp_ingest` (event/click schemas,
consent, streams), `mmp_attrib` (attribution, fraud, SKAdNetwork),
`mmp_providers` (adapters, templates, delivery).

## Why these boundaries

**The tracker is separate because it is the hot path.** It is the only service a
person waits on, and the only one whose slowness is visible to an advertiser's
customers. Keeping it separate means it can be scaled, deployed and hardened on
its own terms — and it holds the narrowest database grants because it is the
most internet-exposed.

**The worker is separate because its work is unbounded.** Attribution loads
candidate clicks; rollups scan windows; the fraud sweep reads a week of data.
None of that can share a process with a 2 ms redirect.

**The API is separate because it is a different risk profile.** Session cookies,
CSRF, RBAC, and long-running exports.

## The hot path

`GET /c/{tracking_code}` does a fixed, tiny amount of work:

1. Dict lookup in the in-process link cache — **no database**.
2. Mint a UUIDv7 click id.
3. Classify the user agent by substring — no parsing library.
4. Hash the IP; the raw address is never stored.
5. Append to a shipping buffer — **not** an await on Redis.
6. 302.

The link cache is the whole active link set in process memory, kept fresh by
`LISTEN`/`NOTIFY` plus a five-minute full resync. Notifications are
fire-and-forget, so the resync is what makes the design safe rather than merely
fast. A tracking-code miss falls through to one indexed lookup and populates the
cache; a **deep link** code miss deliberately does not, because a missing code
is more likely someone probing than a real link, and honouring it would let an
attacker choose how often we query. That makes the notification the only prompt
path for a new code — which is why deep link changes notify too, and why they
had to start doing so.

`Cache-Control: no-store` on the redirect matters more than it looks: without
it, an intermediary can cache the 302 and every click through that proxy reuses
one click id.

## Storage

- **PostgreSQL 16.** `events` and `clicks` are declaratively range-partitioned
  by day with BRIN indexes; retention is dropping a partition, not deleting
  rows.
- **UUIDv7** everywhere, for index locality — ids generated near each other in
  time land near each other in the index.
- **Binary COPY** into a `TEMP` staging table, then
  `INSERT … ON CONFLICT DO NOTHING`. Deduplication is the database's job.
- **Redis Streams** for the queue, with consumer groups.

Partition bounds are written with explicit `+00` and the roles are pinned to
UTC — resolving bounds in a session timezone put data in the wrong partition by
5.5 hours during development.

## Failure behaviour

| Failure | Behaviour |
|---|---|
| Redis unavailable | Redirect still serves; events buffer and are rejected with 503 when full |
| Postgres unavailable | Redirect still serves from cache; ingest fails closed |
| Worker down | Queue accumulates; nothing is lost; backlog metric rises |
| Postback endpoint down | Retried with jittered backoff, then abandoned and recorded |
| Duplicate event | Deduplicated on client-minted `event_id` |
| Duplicate delivery | At-least-once by design; receivers must be idempotent |

## Observability

Prometheus metrics with **deliberately no per-tenant labels** — customer
cardinality in a metrics store is permanent. Structured JSON logs with redaction.
`mmp_pipeline_drift` compares accepted against persisted per hour and is the
metric that catches silent loss.

## Decisions worth knowing

- **Starlette for the tracker, FastAPI for the API.** The tracker does not need
  request validation machinery on a 2 ms path; the API benefits from it.
- **msgspec, not pydantic, on the ingest path.** Measured.
- **`SET LOCAL`, not a connection-per-tenant.** Works under PgBouncer
  transaction pooling.
- **No ORM on the hot path.** Raw asyncpg with hand-written SQL; SQLAlchemy
  models exist for migrations and clarity.
- **An f-string SQL ban** outside `mmp_db`'s reviewed builders, enforced by a
  test. It has caught real code, including mine.
