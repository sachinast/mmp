# 16 — Change Log

**Status:** living. Generated from git history and maintained by hand thereafter.

Format: what changed, and — where it matters — what was wrong before. The
"fixed" entries are the useful part of this document; a changelog of only
features hides the reasons things are shaped as they are.

## Unreleased

Nothing is released. There is no deployed environment and no version tag. The
phases below are development history.


### 2026-09-07

- **Phase 0: foundation, observability and quality gates** (`86d5a7e`)
- **Phase 1: schema, partitioning and row-level security** (`9e91d44`)
- **Phase 2: authentication, organisations, apps and API keys** (`9c62e65`)

### 2026-09-08

- **Phase 3: the ingest pipeline, end to end** (`c2e0ff8`)
- **Phase 4: campaigns, tracking links and click tracking** (`302a7a2`)
- **Phase 5: attribution and Play Install Referrer** (`99ce14f`)
- **Phase 6: sessions, rollups and the analytics API** (`7e90ab9`)
- **Phase 7: the dashboard** (`2695d81`)
- **Phase 9: S2S events, postbacks and webhooks** (`a999401`)
- **Phase 9: postback rule management and webhook configuration** (`c33d46b`)
- **Fix migrations that read live metadata, and guard against it** (`1888690`)
- **Phase 10: reliability, reconciliation, and a security audit that found things** (`809d405`)
- **Phase 11: KMS, consent, erasure and the audit log** (`214ec35`)
- **Provider integration framework** (`67d3baa`)
- **Rule-based fraud signals** (`eb22c45`)
- **Deferred deep links** (`8a30828`)

### 2026-09-09

- **Raw data export** (`59f0ae3`)
- **React Native SDK** (`f2ecae0`)
- **Native modules for the React Native SDK** (`8a9ff6f`)
- **SKAdNetwork postback verification and ingestion** (`6dc59e7`)
- **SKAdNetwork conversion values and reporting** (`4bd50b3`)
- **On-device testing** (`f5f79eb`)


## Notable fixes, by theme

Collected because they recur and are worth not relearning.

### Tests that passed for the wrong reason
- Test fixtures connected as the schema owner, bypassing RLS — every
  cross-tenant assertion was vacuous.
- A `SET LOCAL` tenancy test, a cache-tenant test, an identity-query guard and a
  provider-leak regex all passed without testing what they claimed.
- Boundary tests derived their thresholds from the constant under test, so
  moving a threshold moved the assertion with it.
- Three fraud-sweep tests counted rows globally: they passed alone and failed in
  the suite.

### Data correctness
- `events` and `clicks` had no RLS while `mmp_api` could read them.
- Distinct counts were summed across hourly buckets — "570 installs, 12 unique
  devices". Renamed to `peak_hourly_devices`.
- Partition bounds resolved in the session timezone, placing rows 5.5 hours off.
- A fraud-sweep join fanned out install counts, which would have manufactured a
  flooding finding from arithmetic alone.
- `deep_links.code` was globally unique, so one advertiser could block every
  other from using "summer".

### Silent failures
- `record()` ignored the postback adapter's verdict, so a `200` carrying an
  error counted as delivered.
- A per-integration `event_map` merged rather than replaced, so it could rename
  an event but never exclude one.
- `uname` returns the host architecture on an iOS simulator, so every simulator
  install reported a device model of `arm64`.
- Two Makefile targets had broken skip logic — make gives each recipe line its
  own shell, so `exit 0` skipped nothing.

### Infrastructure and tooling
- 55% of API keys were unparseable: `token_urlsafe` emits the delimiter.
- Redis 8.10 `XREADGROUP … BLOCK` never returns; switched to polling.
- A migration read live ORM metadata and broke the chain from an empty database.
- asyncpg returns `jsonb` as a string; a pool-wide codec breaks binary COPY.
- Cached responses outlived a field rename; cache keys gained a schema version.
- Repeated per-device Redis round trips in the ingest path, twice, both caught
  by the latency gate.
- A mutation harness poisoned its own baseline, and stale `.pyc` files made
  mutants appear to survive that had never been loaded.
