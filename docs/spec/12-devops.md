# 12 — DevOps / Deployment

**Status:** living, and **partly aspirational**. Local development and the
quality gates are real and used daily. There is no deployed environment, no
Terraform, and no CI pipeline configuration in this repository — the gates run
locally via `make`. Treat the deployment section as a specification.

## What is real

**Local development.**

```bash
make setup          # dependencies
make db-create migrate dev-roles
make infra-up       # postgres + redis
make tracker        # :8001
make api            # :8002
make worker
make web
```

**Quality gates.**

```bash
make check          # lint, mypy --strict, bandit, pytest, SDK tests, Swift typecheck
make bench          # latency gate
make audit          # pip-audit --strict
make sdk-ios-device # runs the iOS core on a simulator
make sdk-android    # compiles the Kotlin (needs JDK 17-21)
```

**Migrations.** Alembic, verified from an empty database on every change.

**Scheduled work**, run from the supervised worker rather than cron on a box
nobody remembers:

| Job | Interval |
|---|---|
| Partition maintenance | hourly |
| Usage metering | 60s |
| Rollup refresh | 60s |
| Late-arrival rollups | 6h |
| Postback retries | 15s |
| Reconciliation | hourly |
| Fraud sweep | hourly |

**Verification tooling.** `infra/verify/device_checks.py` replays Apple's real
signed postbacks at a deployed host — the one check that catches a proxy or TLS
terminator altering a request body, which no unit test can reach.

## What is specified but not built

**Deployment topology.** The service split implies the shape: the tracker scales
horizontally and independently (it is stateless apart from its in-process link
cache, which self-heals via `LISTEN`/`NOTIFY` plus a five-minute resync); the
worker scales by consumer group; the API scales on request volume.

**PgBouncer in transaction mode.** The tenancy design already assumes it —
`SET LOCAL` inside a transaction rather than a session variable — so this is a
deployment step, not a code change.

**Egress isolation.** SSRF defence exists in code; the worker should also be
network-isolated so a bypass has an outer layer to fail against. This is
[an open production blocker](../PRODUCTION_CHECKLIST.md).

**CI.** The gates exist as `make` targets and are designed to run unattended.
Wiring them to a CI provider is unstarted.

## Configuration

Environment variables, loaded through `mmp_core.settings` with validation at
startup. A service that cannot resolve its configuration refuses to start rather
than running degraded.

Secrets — the API key pepper, the KMS key id, the session secret — are never
defaulted. There is a test asserting a service will not boot with a placeholder.

## Backups

`make backup-drill` exists. **A backup nobody has restored is not a backup**;
the drill is the point, not the dump.

## Rollback

- **Code:** stateless services, redeploy the previous image.
- **Migrations:** every migration has a tested `downgrade`, verified by round
  trip. Some are destructive by nature — dropping a column loses its data — and
  those say so.
- **Data:** attributions are immutable and superseded rather than updated, so a
  bad attribution run can be reasoned about after the fact.

## Open

1. No CI configuration.
2. No infrastructure-as-code.
3. No deployed environment, so no real SLO.
4. No alerting rules — metrics are exposed, nothing consumes them.
5. No log aggregation configured.
