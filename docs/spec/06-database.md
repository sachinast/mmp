# 06 — Database / ERD

**Status:** describes the built system. The table and column listing below was
extracted from the running database on 2026-09-09, not written from memory.

**26 tables** (excluding daily partitions), **22 with row-level security**,
**2 partitioned** (`events`, `clicks`).

## Entity relationships

```
organizations ─┬─< organization_members >─ users
               ├─< apps ─┬─< api_keys
               │         ├─< campaigns ─< tracking_links ─< clicks
               │         ├─< deep_links
               │         ├─< conversion_mappings
               │         ├─< events
               │         ├─< attributions ──(superseded_by, self)
               │         ├─< consent_states
               │         ├─< fraud_findings
               │         └─< skadnetwork_postbacks
               ├─< postback_rules ─< postback_deliveries
               ├─< provider_integrations
               ├─< webhooks ─< webhook_deliveries
               └─< audit_log
```

Every tenant table carries `organization_id` and is protected by RLS. That
column is redundant on tables reachable through `app_id` — deliberately, because
an RLS policy that has to join to find its tenant is a policy that gets dropped
for performance.

## The invariants that matter

**One install, one attribution.** A partial unique index, not application code:

```sql
CREATE UNIQUE INDEX uq_attributions_install_key_current
    ON attributions (app_id, install_key) WHERE superseded_by IS NULL;
```

The workers consume a queue that redelivers on timeout, so enforcing this in
code would mean enforcing it across racing processes. The database refuses the
duplicate and the losing worker's `ON CONFLICT DO NOTHING` turns a race into a
no-op.

**Attributions are immutable.** A better signal inserts a new row and points the
old one at it through `superseded_by` (a `DEFERRABLE` self-reference, which is
what allows supersede-then-insert in one transaction). A number already reported
to a network can always be reconstructed.

**SKAdNetwork replay protection.** `transaction_id` is globally unique — not
per-app, so a postback cannot be replayed against a *different* app.

**The audit log is hash-chained.** Each entry carries the previous entry's hash,
so a deletion or edit breaks the chain. `GET /v1/privacy/audit/verify` walks it.

## Partitioning

`events` and `clicks` are range-partitioned by day, created ahead by a scheduled
job, with BRIN indexes on the time column. Retention is `DROP PARTITION` — a
constant-time operation that returns disk, unlike a `DELETE` that leaves bloat
and a long-running vacuum.

Bounds are written with explicit `+00` and the roles pinned to UTC. Resolving
bounds in the session timezone (Asia/Kolkata) put rows in the wrong partition by
5.5 hours during development.

## Conventions

- **UUIDv7 primary keys** for index locality.
- **`jsonb` for open-ended data** only: event `properties`, audit `detail`, the
  raw SKAdNetwork `payload`. asyncpg returns `jsonb` as a string, so decoding
  happens at read sites via `mmp_db.jsonfields` — a pool-wide codec would break
  binary COPY.
- **Money as integer minor units.** Never a float.
- **Timestamps are `timestamptz`,** always UTC.
- **Check constraints for closed sets** — attribution method, fraud verdict,
  conversion value range 0–63, coarse value.

## Full column listing

Extracted from the live schema.

```
api_keys :: id, organization_id, app_id, name, kind, key_prefix, key_hash, pepper_version, environment, status, last_used_at, revoked_at, created_at, updated_at
apps :: id, organization_id, name, platform, android_package_name, ios_bundle_id, timezone, status, install_window_days, event_window_days, session_timeout_minutes, created_at, updated_at, consent_mode, apple_app_id
attributions :: id, organization_id, app_id, install_key, anonymous_id, user_id, click_id, campaign_id, tracking_link_id, source, medium, method, installed_at, attributed_at, window_days, expires_at, superseded_by, created_at, updated_at, fraud_score, fraud_verdict, fraud_rules, deep_link
audit_log :: id, organization_id, actor_user_id, action, resource_type, resource_id, detail, previous_hash, entry_hash, created_at
campaigns :: id, organization_id, app_id, name, source, medium, external_campaign_id, status, created_at, updated_at
clicks :: click_id, clicked_at, organization_id, app_id, campaign_id, tracking_link_id, device_hash, ip_hash, country, platform, os_version, device_model, user_agent, sub1, sub2, sub3, is_bot, deep_link
consent_states :: id, organization_id, app_id, anonymous_id, purpose, state, source, expires_at, created_at, updated_at
conversion_mappings :: id, organization_id, app_id, platform, event_name, conversion_value, coarse_value, created_at, updated_at
deep_links :: id, organization_id, app_id, code, destination, fallback_url, created_at, updated_at
events :: event_id, received_at, occurred_at, organization_id, app_id, event_name, anonymous_id, user_id, session_id, platform, os_version, app_version, device_model, country, ip_hash, click_id, revenue_minor, currency, clock_skew_ms, properties
fraud_findings :: id, organization_id, app_id, tracking_link_id, campaign_id, window_start, window_end, rule, severity, detail, evidence, created_at
organization_members :: id, organization_id, user_id, role, created_at, updated_at
organizations :: id, name, slug, timezone, created_at, updated_at
pipeline_audit :: id, app_id, bucket_hour, stage, count, recorded_at
postback_deliveries :: id, organization_id, postback_rule_id, event_id, status, attempt_count, request_url, response_status, response_body, error, created_at, delivered_at, next_retry_at
postback_rules :: id, organization_id, app_id, provider_integration_id, name, trigger_event, method, url_template, body_template, headers_ciphertext, headers_nonce, wrapped_dek, success_status_codes, requires_attribution, is_sandbox, enabled, created_at, updated_at
provider_integrations :: id, organization_id, provider, name, credentials_ciphertext, credentials_nonce, wrapped_dek, key_version, configuration, status, last_health_check_at, created_at, updated_at
rollup_campaign_daily :: organization_id, app_id, bucket_day, campaign_id, clicks, installs, organic_installs, revenue_minor, conversions, updated_at
rollup_clicks_hourly :: organization_id, app_id, bucket_hour, campaign_id, platform, click_count, bot_count, updated_at
rollup_events_hourly :: organization_id, app_id, bucket_hour, event_name, platform, event_count, unique_devices, revenue_minor, updated_at
skadnetwork_postbacks :: id, organization_id, app_id, received_at, version, ad_network_id, apple_app_id, transaction_id, source_identifier, did_win, redownload, fidelity_type, conversion_value, coarse_value, postback_sequence_index, source_app_id, source_domain, payload
tracking_links :: id, organization_id, app_id, campaign_id, tracking_code, name, android_url, ios_url, fallback_url, deep_link_path, status, created_at, updated_at
usage_rollup :: id, organization_id, app_id, bucket_hour, metric, count
users :: id, email, password_hash, name, is_active, last_login_at, created_at, updated_at
webhook_deliveries :: id, organization_id, webhook_id, event_id, event_type, status, attempt_count, request_url, request_body, response_status, response_body, error, created_at, delivered_at, next_retry_at
webhooks :: id, organization_id, url, secret_ciphertext, secret_nonce, wrapped_dek, events, enabled, consecutive_failures, disabled_at, created_at, updated_at
```

## Migrations

Alembic, in `packages/mmp_db/src/mmp_db/migrations/versions/`. Every migration
is verified to apply to an **empty** database, not only as an increment.

One rule learned the hard way: **a migration must never read live ORM
metadata.** Adding a model retroactively changed an already-applied migration
and broke the chain from empty. Collections are inlined, there is an import
guard, and a slow test applies the full chain.
