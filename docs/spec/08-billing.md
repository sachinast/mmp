# 08 — Billing & Subscription Specification

> **STATUS: NOT BUILT.**
>
> Nothing in this document exists in the codebase. There is no plan, no
> subscription, no payment integration, no invoice, and no paywall. Registration
> creates a fully-functional organisation with no limits.
>
> This is a **decision document**. It records what metering already exists,
> which is real, and proposes how billing could sit on top — but the pricing
> model is a commercial decision nobody has made, and the proposal below should
> be treated as one option rather than a plan of record.

## What actually exists

**Usage metering.** The `usage_rollup` table records, per organisation, per app,
per hour, a count per metric. It is populated every 60 seconds by
`refresh_usage` in `mmp_worker/jobs.py`, currently for the `events` metric.

It is deliberately excluded from erasure — billing volume carries no personal
identifier, and deleting it would change a number an advertiser has already been
invoiced on.

That is the whole of it. Metering is built; billing is not.

## Open commercial decisions

These must be answered before any of the rest matters.

1. **What is the billable unit?** Events, attributed installs, MTUs (monthly
   tracked users), or a platform fee? The industry norm is attributed installs
   plus a volume component, but that penalises exactly the accuracy this
   platform sells: refusing to claim a probabilistic attribution *lowers* the
   bill under install-based pricing.
2. **Are organic installs billable?** They are measured and cost the same to
   process.
3. **What happens at the limit?** Refusing ingest loses data permanently and
   silently breaks a customer's measurement. Overage billing avoids that but
   creates surprise invoices.
4. **Who pays — advertiser or network?** Some MMPs bill networks for postback
   delivery.

Question 3 has a technical consequence the others do not: whatever is decided,
**do not drop events at a plan limit.** A dropped install cannot be recovered,
the customer discovers it weeks later in a reconciliation, and the platform's
entire value is that its numbers are trustworthy.

## Proposed design (not built)

If billing is added, the shape that fits the existing system:

**Metering.** Extend `usage_rollup` metrics beyond `events` — `attributed_installs`,
`postback_deliveries`. The table and job already support this; it is a matter of
adding rows, not a new subsystem.

**Plans and subscriptions.** New tables `plans` and `subscriptions`, both
org-scoped and under RLS like everything else. A subscription references a plan
and carries the period boundaries.

**Enforcement, in order of preference:**

1. **Soft** — record overage, invoice it, alert the customer. Never lose data.
2. **Degrade** — keep ingesting, pause postbacks and exports. The measurement
   stays correct; the conveniences stop.
3. **Hard stop** — only after explicit written notice, and never on ingest.

**Payment.** No provider chosen. Whichever is used, the same rule as every other
secret here applies: card data never touches this platform, only a provider
token, and that token is envelope-encrypted like integration credentials.

**Invoicing** reads from `usage_rollup`, which is immutable and already excluded
from erasure — so an invoice can always be reconstructed from the data it was
computed from.

## Security requirements, whatever is chosen

- Billing endpoints require `owner`, not `admin`.
- Every plan change writes to the hash-chained audit log.
- Usage figures are computed server-side from `usage_rollup` and never accepted
  from a client.
- A billing outage must not stop ingest. Fail open on measurement, closed on
  spending.

## Next step

This document cannot progress without the four commercial answers above. The
engineering work behind any of them is small; the decisions are not.
