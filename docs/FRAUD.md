# Fraud detection

## What this is

A set of stated rules with stated thresholds, evaluated in two places: inline at
attribution for signals that need only one install, and hourly over a trailing
seven-day window for signals that need a population.

The rules live in `packages/mmp_attrib/src/mmp_attrib/fraud.py` as pure
functions — no database, no clock, no network. Fraud accusations get argued
about, sometimes with a lawyer in the room, so an assessment has to be
reproducible exactly, months later, from stored inputs.

## What it deliberately is not

**Not a model.** A trained classifier would very likely catch more. It also
could not answer *"why did you stop paying us for these 40,000 installs"* with
anything the network could act on. A rule can, and a network that knows the rule
can stop breaking it — which is the actual goal. Detection that cannot be
explained just moves the fraud somewhere less visible.

**Not an enforcement mechanism.** Nothing here changes an attribution, hides a
row, or alters a number. A flagged install is still attributed and still counted
in every analytics path. Two reasons: an advertiser whose traffic is being
injected should not also lose the installs they genuinely paid for, and a system
that quietly drops data makes its own errors invisible.

Whether a flagged conversion is billed, or is forwarded to a network, is a
commercial decision. This subsystem supplies the evidence and stops there. **If
you want flagged conversions withheld from postbacks, that is a deliberate
change to `mmp_worker.postbacks` and it is not currently made.**

## The rules

| Rule | Where | Severity | Fires when |
|---|---|---|---|
| `click_injection` | per install | CRITICAL | click-to-install under 10s, or a click timestamped after its own install |
| `late_conversion` | per install | LOW | install more than 7 days after the click |
| `bot_click` | per install | HIGH | the winning click came from a known-bot user agent |
| `click_flooding` | per link | CRITICAL | ≥50 attributed installs and ≥60% of them converted late |
| `click_farm` | per link | HIGH | ≥75 distinct devices behind one IP hash |
| `device_replay` | per link | MEDIUM | one device credited with ≥6 installs of the app |

Scores sum; ≥25 is `suspicious`, ≥100 is `fraudulent`. The severity gaps are
load-bearing: two LOW hints must not add up to a verdict, because two weak
independent hints are not one strong finding.

Thresholds are set where a legitimate explanation is genuinely hard to
construct, not where the signal first appears. Wrongly flagging a real network
costs a partnership; missing some fraud costs money that is already mostly lost.

`tests/test_fraud_rules.py` pins every threshold as a literal, so moving one
breaks a named test rather than silently following the constant.

## Where the data goes

- Per-install: `attributions.fraud_score`, `.fraud_verdict`, `.fraud_rules`.
  `NOT NULL`, defaulting to clean — there is no "unassessed" state, because a
  nullable verdict would leave every reader to invent a meaning for missing.
- Per-link: the `fraud_findings` table, one row per link per rule per window,
  upserted so a re-run corrects rather than duplicates, and cleared when a link
  stops misbehaving. A finding left standing after the behaviour stopped is
  worse than no finding at all.
- Read via `GET /v1/fraud/findings` and `GET /v1/fraud/installs`. Read-only by
  design; there is a test asserting no write path exists.

## Known gaps

**Datacenter clicks are not detected.** Clicks from hosting-provider networks
are a strong signal and are deliberately absent: IPs are hashed at the edge for
privacy, so the raw address is gone before the rules could see it. Classifying
it would have to happen in the tracker against a maintained CIDR list — real
work with a real data dependency. A rule that can never fire is worse than an
absent one, so it is not shipped.

**No cross-app or cross-advertiser correlation.** A device farm serving many
advertisers is invisible to per-app rules. This needs a global device view,
which has privacy consequences that should be decided deliberately.

**Thresholds are global, not per-app.** A hyper-casual game and a banking app
have very different legitimate click-to-install distributions. Per-app tuning is
the obvious next step and is not built.

**Nothing is benchmarked against known-fraudulent traffic.** The thresholds are
reasoned, not measured. They should be revisited against real labelled data
before anyone is accused on the strength of them alone.
