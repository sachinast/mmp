# 01 — Product Requirements Document

**Status:** describes the built system, as of 2026-09-09.

## What this is

A mobile measurement platform (MMP). It tells an advertiser which of their paid
campaigns produced which app installs, and what those users did afterwards.

That sentence hides the actual difficulty. Ad networks are paid on the installs
they claim; the platform decides which claims are true. Every design decision
below follows from being the referee between parties with money at stake.

## The problem

An advertiser spends across several ad networks. Each network reports installs
it believes it caused. Those reports overlap, disagree, and — because networks
are paid on them — are not disinterested. Without an independent measurement
layer the advertiser is paying several parties for the same install and has no
way to tell.

Platform privacy changes made this harder rather than easier. iOS removed the
device identifier that made attribution straightforward; Android is following.
Attribution now has to work from weaker signals without falling back to
fingerprinting.

## Who it serves

**The advertiser** (app publisher) is the customer. They want to know which
spend produced which outcome, in numbers they can defend to a finance team.

**The ad network** is not the customer but must be served: they need conversion
data fed back to optimise, and they need to trust the referee.

**The developer** integrating the SDK wants it to be invisible — no crashes, no
battery drain, no unexplained data collection.

## What it does

1. **Tracking links.** Each campaign gets a link. A click redirects to the app
   store and records the click.
2. **Ingest.** The SDK reports installs and events.
3. **Attribution.** An install is matched to at most one click, deterministically.
4. **Fraud signals.** Rule-based scoring flags implausible attributions.
5. **Postbacks.** Conversions are forwarded to the network that earned them.
6. **Reporting.** Dashboard, analytics API, raw export.
7. **Privacy.** Consent, erasure, data minimisation, a tamper-evident audit log.
8. **SKAdNetwork.** Apple's iOS attribution, verified and reported separately.

## Requirements that shaped the build

### Accuracy is the product

**Deterministic attribution only.** Referrer, click id, or device match — in
that order. If none matches, the install is organic. There is no probabilistic
tier, deliberately: an attribution nobody can explain is one nobody should be
charged for. This costs match rate and it is the right trade.

**One install, one attribution.** Enforced by a partial unique index in the
database, not by application code, because the workers consume a queue that
redelivers on timeout.

**Attributions are immutable.** A better signal inserts a new row and points the
old one at it. A number already reported to a network can always be
reconstructed.

### Speed is a commercial requirement

The redirect is the only endpoint whose slowness is visible to an advertiser's
*customers*. Every hundred milliseconds is a share of them who leave. Current:
**p50 2.4 ms, ingest p50 3.1 ms** over loopback, gated in CI.

The redirect does no database work — the link set lives in process memory,
refreshed by `LISTEN`/`NOTIFY` with a periodic resync.

### Privacy is not a feature

Raw advertising IDs and IP addresses are hashed at the edge and the raw values
discarded. No fingerprinting. Consent is enforced at ingest by dropping fields
that serve a denied purpose, so a denied purpose means the field never existed.

### Multi-tenancy must not depend on remembering

Row-level security on 22 tables, `ENABLE` **and** `FORCE`. A forgotten `WHERE`
returns nothing rather than someone else's data.

## What it deliberately does not do

- **No probabilistic attribution / fingerprinting.** See above.
- **No machine learning.** Fraud detection is rules with stated thresholds,
  because a network that is accused must be told what rule it broke. See
  [13 — AI Specification](13-ai.md).
- **No ad serving.** This platform measures; it does not buy or serve ads.
- **No cross-app identity graph.** The obvious way to raise match rates, and the
  thing the privacy model exists to avoid.

## Success measures

| Measure | Target | Current |
|---|---|---|
| Redirect p50 | < 10 ms | 2.4 ms (loopback) |
| Ingest p50 | < 20 ms | 3.1 ms (loopback) |
| Attribution determinism | 100% explainable | 100% |
| Cross-tenant leakage | zero | zero found; RLS on 22 tables |
| Postback delivery | at-least-once, deduplicated | implemented |

The latency figures are from a loopback benchmark on one machine, which is not
an SLO. Verification from separate hardware under load is
[an open production blocker](../PRODUCTION_CHECKLIST.md).

## Open questions

These are product decisions nobody has made:

1. **Pricing and billing.** Metering exists (`usage_rollup`); nothing bills. See
   [08](08-billing.md).
2. **Which ad networks ship as first-class adapters.** The framework exists with
   two generic adapters; no real network is named in the codebase on purpose.
3. **Self-serve or sales-led onboarding.** Registration works; there is no
   trial, plan, or paywall.
