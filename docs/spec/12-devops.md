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

**CI** runs on GitHub Actions (`.github/workflows/ci.yml`), on every push to
`main` and every pull request:

- `check` (ubuntu) — lint, `mypy --strict`, bandit, `pip-audit`, migrations
  against a real PostgreSQL, the full pytest suite, and the SDK's typecheck and
  tests.
- `android` (ubuntu) — compiles and lints the Kotlin against a real Android SDK.
  This is the only place the Kotlin is compiled at all.

Two targets skip when their toolchain is absent, so `make check` still works on
a machine without Node or an Android SDK. That is exactly wrong in CI, where a
broken setup step would leave the job green and nothing would run — so
`MMP_REQUIRE_SDK` and `MMP_REQUIRE_ANDROID` turn those skips into failures, and
CI sets both.

**Not in CI:** `make sdk-ios` and `make sdk-ios-device` need a macOS runner,
which bills at ten times the rate of Linux on a private repository. They run in
`make check` locally on every change. Enabling a macOS job is a cost decision,
not a technical one.

`make bench` is also absent: it compares latency against a recorded baseline,
and a shared runner's timing noise would make it flake rather than inform.

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

1. No infrastructure-as-code.
2. No deployed environment, so no real SLO.
3. No alerting rules — metrics are exposed, nothing consumes them.
4. No log aggregation configured.
