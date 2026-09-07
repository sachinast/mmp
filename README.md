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
make tracker   # :8001  event ingestion and click redirects
make api       # :8002  auth, orgs, apps, campaigns, analytics
make web       # :8003  dashboard
make worker    #        background processing
```

Every HTTP service answers `/health` (is the process alive) and `/ready` (should
it receive traffic — runs dependency probes, reports draining during shutdown).

## Quality gates

```bash
make check     # lint + types + security + tests, exactly what CI runs
make fmt       # autofix
make audit     # dependency CVE scan against the lockfile
```

## Credentials

| Secret | At rest | Why |
| --- | --- | --- |
| User password | argon2id | Verified once per login, behind a rate limiter. Slow is the feature. |
| API key | HMAC-SHA256 under a KMS pepper | Verified on **every ingest request**. A slow hash here would be a self-inflicted denial of service; 256 bits of entropy does the work instead. |
| Partner credentials | AES-256-GCM, KMS-wrapped DEK | Must be recoverable. Bound to the owning organisation, so a row copied to another tenant fails to decrypt. |
| Session | Opaque token in Redis | Revocable. A JWT would make logout a lie. |

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
- [ ] Phase 3 — ingest pipeline end to end
- [ ] Phase 4 — campaigns, links, click tracking
- [ ] Phase 5 — attribution and Play Install Referrer
- [ ] Phase 6 — sessions, rollups, analytics API
- [ ] Phase 7 — dashboard
- [ ] Phase 8 — React Native SDK
- [ ] Phase 9 — S2S, postbacks, webhooks
- [ ] Phase 10 — reliability and security audit
- [ ] Phase 11 — privacy, consent, provider framework
- [ ] Phase 12 — deep links, fraud signals, export
- [ ] Phase 13 — iOS attribution (parallel workstream)
