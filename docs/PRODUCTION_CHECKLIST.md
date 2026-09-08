# Production readiness checklist

What has to be true before this platform carries a customer's traffic. Items are
marked by what is actually done, not by what is planned.

- **Done** — implemented and covered by a test that fails if it is removed.
- **Open** — not done. Blocking items are marked so.

The blocking items are listed first, because a checklist whose failures are
buried in the middle is a checklist that gets skimmed.

---

## Blocking — the platform must not take production traffic until these are done

| | Item | Why it blocks |
| --- | --- | --- |
| **Open** | **A real KMS provider for credential wrapping** | Partner API tokens and webhook signing secrets are encrypted under a key derived from configuration, in the same process as the data. That is the property KMS exists to provide. `provider_from_settings` refuses to start when `MMP_ENVIRONMENT=prod`, so this is enforced rather than remembered. |
| **Open** | **Egress network isolation for outbound delivery** | The SSRF guard in `mmp_core.outbound` is defence in depth. The other half — a network segment with no route to internal services — lives in infrastructure and is not in this repository. |
| **Open** | **Verify the published latency SLO** | The integration docs commit to p99 < 120 ms. The in-repo benchmark is a regression gate against a recorded baseline and says so; the SLO needs `infra/load/ingest.k6.js` run from separate hardware against a real deployment. Ad networks ask for this number on day one. |
| **Open** | **A cost-per-million-events target** | Without one, engineering optimises for nothing and the product may be priced below what it costs to run. Raised in the build plan and still unanswered. |
| **Open** | **Named security contact** | `docs/SECURITY.md` has no reporting address. |

---

## Data and schema

| | Item | Notes |
| --- | --- | --- |
| Done | Migrations apply to an empty database | Enforced by a test that creates one and runs the whole chain — the only check that catches a chain working incrementally and failing from empty. |
| Done | Migrations never read live metadata | Import guard over every migration. A migration deriving its table list from the ORM changed behaviour retroactively; see the commit that fixed it. |
| Done | Partition boundaries are explicitly UTC | Bare date literals resolve in the session time zone; created from a machine set to `Asia/Kolkata` every partition was 5½ hours off the day it named. |
| Done | Partition creation runs a week ahead | If the job stops there is a week of warning before inserts fail, not a day. |
| Done | Retention drops partitions rather than deleting rows | Guarded by a 30-day floor and defaults to dry run. |
| Done | Backup restore drill | `make backup-drill` dumps, restores and compares row counts, schema **and RLS policy count** — a restore that silently drops RLS leaves a database that works and leaks. |
| **Open** | Automated backups with a tested retention | The drill exists; scheduling it and the backups themselves is deployment work. |
| **Open** | Point-in-time recovery | Not configured. Decide the acceptable RPO before, not after. |

## Access control

| | Item | Notes |
| --- | --- | --- |
| Done | RLS on every tenant table, `ENABLE` and `FORCE` | Including `events` and `clicks`, which the audit found unprotected. |
| Done | Four least-privilege roles, no `SUPERUSER`, no `BYPASSRLS` | The worker's cross-tenant access is a named policy, visible in the catalogue. |
| Done | Request-path tenancy always via `SET LOCAL` | The PgBouncer pooling trap; asserted directly. |
| Done | Application never connects as the schema owner | A superuser bypasses RLS even with `FORCE`. Test fixtures connect as the real roles for this reason. |
| **Open** | RLS on `users` and `organization_members` | A documented exception with a static guard. See `docs/SECURITY.md` §1. |
| **Open** | Rate limiting on the business API | Login is limited per account and per address. The rest is not. |

## Secrets

| | Item | Notes |
| --- | --- | --- |
| Done | No secret in the repository | `.env` is gitignored; settings reject placeholder values at boot. |
| Done | API keys stored as HMAC under a pepper, shown once | Peppers must come from KMS in production, not the environment. |
| Done | Rotation implemented for API keys and webhook secrets | Rotation *overlaps* for API keys so an SDK in the field is never left without a working credential. |
| **Open** | Rotation schedule and reminders | Implemented, unscheduled. |
| **Open** | Peppers sourced from KMS at boot | Currently environment variables. Same blocker as credential wrapping. |

## Observability

| | Item | Notes |
| --- | --- | --- |
| Done | Structured JSON logs with request and correlation IDs | Correlation propagates through the queue into the workers. |
| Done | PII redaction in logs, with a test | |
| Done | `/health` and `/ready` distinguished | `/health` touches nothing external, so a Redis outage does not get every healthy pod killed. |
| Done | Graceful drain: report not-ready, wait, then close | What makes a rolling deploy lossless. |
| Done | Prometheus metrics on `/metrics` | Deliberately no per-tenant labels — that is how a metrics bill becomes a surprise. Per-tenant numbers live in the rollups. |
| Done | Stream backlog and pending gauges | The number that says ingestion has stopped. |
| Done | Reconciliation job counting both ends | Silent loss is the only failure that produces no other signal. |
| **Open** | Alerting rules | The metrics exist; nothing pages. Suggested first three: stream backlog rising with a zero write rate, reconciliation drift above tolerance, redirect p99 above the SLO. |
| **Open** | Distributed tracing | Correlation IDs are propagated but nothing collects spans. |
| **Open** | Log aggregation and retention | |

## Application security

| | Item | Notes |
| --- | --- | --- |
| Done | SSRF guard with destination pinning, no redirects | Tested against DNS rebinding. |
| Done | Postback templates are not a template engine | Tested against the standard SSTI corpus. |
| Done | S2S signing with timestamp and nonce replay protection | |
| Done | Webhook signing, with the verification snippet beside the signing code | |
| Done | Payload and decompression caps | A gzip bomb is otherwise a memory-exhaustion primitive. |
| Done | CSRF on state-changing requests; httpOnly SameSite session cookie | |
| Done | Strict CSP on the dashboard | `default-src 'none'` is possible because nothing external is loaded. |
| Done | Security headers on every service | |
| **Open** | Penetration test | Everything above is self-assessed. |
| **Open** | Dependency update policy | `pip-audit` gates CI; nothing drives routine upgrades. |

## Operations

| | Item | Notes |
| --- | --- | --- |
| Done | API versioning and deprecation policy | Written in Phase 0, before any external endpoint existed. |
| Done | Usage metering from day one | Metering data cannot be reconstructed retroactively. |
| **Open** | Runbooks | Specifically: stream backlog growing, reconciliation drift, a partner endpoint failing en masse, a webhook auto-disabled. |
| **Open** | On-call rotation and escalation | |
| **Open** | Load test against production-shaped infrastructure | |
| **Open** | Multi-region / data residency answer | EU advertisers ask on day one, not after scale. |

---

## Before each deploy

1. `make check` — lint, types, security scan, tests.
2. `make audit` — dependency CVEs against the hash-pinned lockfile.
3. `make bench` — latency regression gate against the recorded baseline.
4. `make migrate` on a **copy** of production first, and time it. A migration
   that takes a lock for four minutes is a four-minute outage.
5. Confirm the deploy is rolling and the drain grace is longer than the longest
   in-flight request.

## After each deploy

1. Watch `mmp_stream_backlog` for five minutes. A rising backlog with a healthy
   write rate is capacity; with a zero write rate it is an outage.
2. Check `mmp_events_written_total` is still increasing.
3. Check the next reconciliation run reports drift within tolerance.
