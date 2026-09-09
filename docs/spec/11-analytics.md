# 11 — Analytics & Event Tracking

**Status:** living. Last reviewed 2026-09-09.

## The event model

One flat event with a name, a device, a time, and open-ended `properties`.
Deliberately not a rigid schema per event type: an advertiser's funnel is theirs
to define, and a platform that requires schema registration before an event can
be sent becomes a platform where events go untracked.

Required: `event_name`, `anonymous_id`. Everything else is optional except
`currency` when `revenue_minor` is set.

Limits, enforced identically in the SDK and the server (and asserted equal by a
contract test): event name 120 chars, ids 255, properties 16 KB, 100 events per
batch.

## Reserved names

`install`, `login`, `signup`, `consent_update`. The SDK sends all four itself
and refuses them from `track()` — an app sending one by hand corrupts state it
does not own. `install` drives attribution, `login`/`signup` drive identity
resolution, `consent_update` carries a consent decision.

**Matched on a folded form**, not literally: lowercased with separators
removed. `Install`, `install`, `Sign-Up` and `sign up` all reach the handling
they obviously intend.

This is not tidiness. Every check used to be an exact match while validation
only checked length, so an app sending `Install` was accepted, stored, and
never attributed — no error anywhere, events arriving normally, and an install
count of zero. A measurement platform cannot afford a failure with no signal,
and "the documentation said lowercase" is no defence when the platform had
every opportunity to understand what was meant.

Only whole names fold: `signup_abandoned` is its own event, not a sign-up.

The name is **stored exactly as sent**. Folding changes what the platform
recognises, never what it reports: an app that calls its event `Purchase` sees
`Purchase` in its reports and its exports.

## Sessions

Assigned server-side at ingest, 30-minute inactivity window (matching the
convention, so numbers here compare with numbers from elsewhere without a
footnote). One Redis round trip per distinct device per batch, pipelined — a
per-device round trip was caught twice by the latency gate.

## Aggregation

Three rollups, refreshed every 60 seconds over a trailing window, plus a
six-hourly pass for late arrivals:

- `rollup_events_hourly`
- `rollup_clicks_hourly` (bots counted separately, never blended)
- `rollup_campaign_daily`

The dashboard reads rollups, never raw events.

**A counting rule learned the hard way:** distinct counts cannot be summed
across buckets. An early version reported "570 installs, 12 unique devices" by
adding hourly distinct counts. The field is now named `peak_hourly_devices`,
which is what it actually is.

## Pipeline integrity

`pipeline_audit` records accepted-versus-persisted per app per hour, and
`mmp_pipeline_drift` exposes the difference. This is the metric that catches
silent loss — the failure mode where everything looks healthy and events simply
stop arriving.

## Metrics exposed

`mmp_events_accepted_total`, `mmp_events_rejected_total`,
`mmp_events_written_total`, `mmp_events_duplicate_total`, `mmp_stream_backlog`,
`mmp_stream_pending`, `mmp_redirects_total`, `mmp_redirect_duration_seconds`,
`mmp_attributions_total`, `mmp_fraud_verdicts_total`, `mmp_fraud_findings_total`,
`mmp_skan_postbacks_total`, `mmp_deliveries_total`,
`mmp_delivery_duration_seconds`, `mmp_pipeline_drift`.

**No per-tenant labels, anywhere.** Prometheus keeps cardinality forever, and a
label an anonymous caller can influence — as on the public SKAdNetwork endpoint
— is a way to blow it up from the internet.

## Reporting caveats that ship with the data

- SKAdNetwork figures carry a caveat string **in the response body**, so the
  warning survives a dashboard rewrite.
- Withheld SKAdNetwork conversion values are reported as `suppressed` rather
  than averaged away.
- Flagged-fraud installs are **still counted** in every analytics path. A fraud
  verdict never silently moves anyone's numbers.

## Caching

Analytics responses are cached with a `CACHE_SCHEMA_VERSION` in the key. Without
it, a field rename served 500s from cache until the TTL expired.

## Open

- Funnels, cohorts and retention are not built.
- No custom dashboards or saved reports.
- No anomaly alerting on a customer's own metrics.
