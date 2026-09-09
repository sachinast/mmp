# 15 — Product Roadmap

**Status:** living. Last reviewed 2026-09-09.

Ordered by what blocks what, not by appeal. Nothing below has a committed date —
dates would be invented.

## Blocking production

Nothing should carry real customer traffic until these are done. Three of the
four are not engineering tasks, which is why they have stayed open.

1. **Egress network isolation.** SSRF defence exists in code; the worker is not
   network-isolated at the infrastructure level. Defence in depth is missing its
   outer layer.
2. **SLO verification from separate hardware.** Every latency figure quoted
   anywhere in these documents is a loopback benchmark on one machine. That is
   not an SLO. Needs k6 against a deployed environment.
3. **A cost-per-million-events target.** Nobody has set one, so nothing can be
   judged too expensive.
4. **A named security contact.** `SECURITY.md` still has a placeholder.

## Blocking a first customer

5. **Device testing.** No postback from a real handset, no React Native app
   built end to end, the Kotlin uncompiled. See
   [DEVICE_TESTING.md](../DEVICE_TESTING.md) for exactly what needs doing.
6. **At least one real ad network adapter.** The framework is built and no
   network is integrated, so there is currently nobody to send conversions to.
7. **Billing, or an explicit decision not to charge yet.** Metering exists;
   billing does not. Four commercial questions block it — see
   [08](08-billing.md).
8. **Dashboard coverage for fraud, SKAdNetwork, deep links and export.** All are
   API-only, which is not a product for a marketer.

## Worth doing next

9. **Per-app fraud thresholds.** Global today. Needs labelled data, which does
   not exist — collecting it is the prerequisite, not the modelling.
10. **SKAdNetwork conversion-window stitching.** Version 4 sends up to three
    postbacks per install; they are stored individually and nothing combines
    them into a view of a user's progression.
11. **Datacenter-IP fraud detection.** A strong signal, deliberately absent: IPs
    are hashed at the edge, so classification must happen in the tracker against
    a maintained CIDR list. Real work with a real data dependency.
12. **CI configuration.** The gates exist as `make` targets and run unattended;
    nothing runs them automatically.
13. **A machine-to-machine read credential.** The management API assumes a
    session, which suits a dashboard and not a partner integration.

## Later

14. Funnels, cohorts, retention.
15. Web attribution (currently mobile only).
16. Additional SDKs — native iOS, native Android, Flutter, Unity.
17. Self-serve onboarding with a trial.
18. Anomaly alerting on customer metrics.

## Explicitly not planned

Recorded so they are not proposed again without a reason:

- Probabilistic attribution or fingerprinting.
- A cross-app identity graph.
- ML-based fraud *decisions* (tuning is fine — see [13](13-ai.md)).
- Ad serving or bidding.
- LLMs anywhere in the measurement path.

## How this list is maintained

An item leaves this document when it is built, tested, mutation-tested, gated
and committed — or when it is explicitly rejected, in which case it moves to the
section above with the reason. Items do not silently disappear.
