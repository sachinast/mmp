# 02 — MVP & Product Scope

**Status:** describes the built system, as of 2026-09-09.

## Shipped

Thirteen phases, 22 commits. Each phase was built, tested, mutation-tested and
gated before the next began.

| Area | State | Where |
|---|---|---|
| Ingest pipeline | Complete | `services/tracker`, `packages/mmp_ingest` |
| Click tracking & redirect | Complete | `services/tracker/redirect.py` |
| Attribution | Complete | `packages/mmp_attrib` |
| Campaigns & tracking links | Complete | `routes/campaigns.py` |
| Auth, orgs, RBAC | Complete | `routes/auth.py`, `deps.py` |
| API keys | Complete | `routes/keys.py` |
| Sessions & rollups | Complete | `mmp_worker/rollups.py` |
| Analytics API | Complete | `routes/analytics.py` |
| Dashboard | Complete | `services/web` |
| S2S events | Complete | `tracker/s2s.py` |
| Postbacks | Complete | `mmp_worker/postbacks.py` |
| Webhooks | Complete | `mmp_worker/webhook_sender.py` |
| Provider adapters | Framework only | `packages/mmp_providers` |
| Consent & erasure | Complete | `mmp_ingest/consent.py`, `mmp_db/erasure.py` |
| Audit log | Complete | `mmp_db/audit.py` |
| Fraud signals | Complete | `mmp_attrib/fraud.py` |
| Deferred deep links | Complete | `tracker/deferred.py` |
| Raw export | Complete | `routes/exports.py` |
| React Native SDK | Complete (JS) | `sdks/react-native` |
| Native modules | **Written, partly unverified** | `sdks/react-native/ios`, `/android` |
| SKAdNetwork | Complete | `mmp_attrib/skadnetwork.py` |

## Explicitly out of scope

Decided against, with reasons, rather than merely not done:

- **Probabilistic attribution / fingerprinting.** Raises match rate; produces
  attributions that cannot be defended and that Apple's rules prohibit for
  cross-app use.
- **Cross-app identity graph.** Same reason.
- **Ad serving or bidding.** Different product.
- **ML-based fraud scoring.** See [13](13-ai.md) for why, and what it would take.
- **Real ad-network adapters.** The framework is built; naming a network bakes
  its quirks into the core. A test asserts no network name appears outside the
  adapter directory.

## Not done, and blocking production

From [PRODUCTION_CHECKLIST.md](../PRODUCTION_CHECKLIST.md):

1. **Egress network isolation.** Outbound postbacks resolve-and-pin against
   blocked ranges, but the worker is not network-isolated at the infrastructure
   level. Defence in depth is missing its outer layer.
2. **SLO verification from separate hardware.** All latency numbers are
   loopback on one machine. They are not an SLO.
3. **A cost-per-million-events target.** Nobody has set one, so nothing can be
   judged too expensive.
4. **A named security contact.** `SECURITY.md` has a placeholder.

## Not done, and merely incomplete

- **Dashboard.** Covers every reporting and configuration view, onboarding, and
  connecting a network. No flow is API-only any more.
- **Android native module never run.** It compiles and lints in CI against a
  real Android SDK, but no build of it has executed on a device or emulator.
- **No device testing.** No postback from a real handset, no RN app built end to
  end.
- **Conversion-window stitching.** SKAdNetwork 4 sends up to three postbacks per
  install; they are stored individually and not combined.
- **Per-app fraud thresholds.** Global today. A hyper-casual game and a banking
  app have very different legitimate distributions.

## Definition of done, as practised

A phase was not complete until:

1. Tests pass.
2. **Mutation testing** passes — the control is deliberately broken and the
   suite must react. This repeatedly found tests that passed for the wrong
   reason.
3. `make check` (lint, types, bandit, tests), `make bench`, `make audit` pass.
4. Migrations verified from an empty database.
5. Committed with the reasoning, including what went wrong.
