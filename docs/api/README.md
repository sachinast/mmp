# API reference

Generated from the running service on 2026-09-09. `openapi.json` in this
directory is the machine-readable schema — point your client generator at it
rather than hand-writing a client.

There are **two APIs** and they are not interchangeable.

| | Tracker | Management API |
|---|---|---|
| Purpose | High-volume device traffic | Configuration and reporting |
| Auth | App key, or Apple's signature | Session cookie + CSRF |
| Audience | Your app, your servers | Your dashboard, your ops |
| Latency | Milliseconds, gated | Ordinary |

New to this? Start with the [quickstart](QUICKSTART.md).

## Tracker

The endpoints your app and servers call. High volume, minimal surface.

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `POST` | `/v1/events` | Ingest events from an SDK | App key (Bearer) |
| `POST` | `/v1/s2s/events` | Ingest events server-to-server | App key + HMAC signature |
| `GET` | `/c/{tracking_code}` | Click redirect | None (public) |
| `POST` | `/v1/deeplink/resolve` | Deferred deep link handshake | App key (Bearer) |
| `GET` | `/v1/skan/conversion-values` | Conversion value mapping for the SDK | App key (Bearer) |
| `POST` | `/.well-known/skadnetwork/report-attribution` | Apple SKAdNetwork postback | Apple's ECDSA signature |

## Management API

Session-authenticated. Every mutating request needs an `x-csrf-token` header
carrying the value of the `mmp_csrf` cookie.

### Authentication

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/auth/login` | Login |
| `POST` | `/v1/auth/logout` | Logout |
| `POST` | `/v1/auth/logout-everywhere` | Logout Everywhere |
| `GET` | `/v1/auth/me` | Me |
| `POST` | `/v1/auth/register` | Register |

### Organisations & members

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/organizations` | List Organizations |
| `POST` | `/v1/organizations` | Create Organization |
| `GET` | `/v1/organizations/members` | List Members |
| `POST` | `/v1/organizations/members` | Add Member |
| `DELETE` | `/v1/organizations/members/{user_id}` | Remove Member |
| `POST` | `/v1/organizations/{organization_id}/switch` | Switch Organization |

### Apps

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/apps` | List Apps |
| `POST` | `/v1/apps` | Create App |
| `DELETE` | `/v1/apps/{app_id}` | Disable App |
| `GET` | `/v1/apps/{app_id}` | Get App |
| `PATCH` | `/v1/apps/{app_id}` | Update App |

### API keys

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/apps/{app_id}/keys` | List Keys |
| `POST` | `/v1/apps/{app_id}/keys` | Create Key |
| `DELETE` | `/v1/apps/{app_id}/keys/{key_id}` | Revoke Key |
| `POST` | `/v1/apps/{app_id}/keys/{key_id}/rotate` | Rotate Key |

### Campaigns & tracking links

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/campaigns` | List Campaigns |
| `POST` | `/v1/campaigns` | Create Campaign |
| `GET` | `/v1/tracking-links` | List Links |
| `POST` | `/v1/tracking-links` | Create Link |
| `DELETE` | `/v1/tracking-links/{link_id}` | Disable Link |
| `PATCH` | `/v1/tracking-links/{link_id}` | Update Link |

### Attribution

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/attributions/lookup` | Lookup Device |
| `GET` | `/v1/attributions/summary` | Summary |

### Analytics

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/analytics/campaigns` | Campaigns |
| `GET` | `/v1/analytics/events` | Events |
| `GET` | `/v1/analytics/overview` | Overview |

### Postbacks

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/postback-deliveries` | List Deliveries |
| `POST` | `/v1/postback-deliveries/{delivery_id}/retry` | Retry Delivery |
| `GET` | `/v1/postback-rules` | List Rules |
| `POST` | `/v1/postback-rules` | Create Rule |
| `DELETE` | `/v1/postback-rules/{rule_id}` | Disable Rule |
| `PATCH` | `/v1/postback-rules/{rule_id}` | Update Rule |
| `GET` | `/v1/postback-rules/{rule_id}/preview` | Preview Rule |

### Webhooks

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/webhooks` | List Webhooks |
| `POST` | `/v1/webhooks` | Create Webhook |
| `GET` | `/v1/webhooks/events` | Available Events |
| `DELETE` | `/v1/webhooks/{webhook_id}` | Delete Webhook |
| `PATCH` | `/v1/webhooks/{webhook_id}` | Update Webhook |
| `GET` | `/v1/webhooks/{webhook_id}/deliveries` | List Deliveries |
| `POST` | `/v1/webhooks/{webhook_id}/rotate` | Rotate Secret |

### Provider integrations

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/integrations` | List Integrations |
| `POST` | `/v1/integrations` | Create Integration |
| `DELETE` | `/v1/integrations/{integration_id}` | Delete Integration |
| `PATCH` | `/v1/integrations/{integration_id}` | Update Integration |
| `GET` | `/v1/integrations/{integration_id}/capabilities` | Integration Capabilities |
| `GET` | `/v1/providers` | List Providers |

### Deep links

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/deep-links` | List Deep Links |
| `POST` | `/v1/deep-links` | Create Deep Link |
| `DELETE` | `/v1/deep-links/{deep_link_id}` | Delete Deep Link |

### SKAdNetwork

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/skan/conversion-values` | List Conversion Values |
| `PUT` | `/v1/skan/conversion-values` | Set Conversion Value |
| `DELETE` | `/v1/skan/conversion-values/{mapping_id}` | Delete Conversion Value |
| `GET` | `/v1/skan/postbacks` | List Postbacks |
| `GET` | `/v1/skan/summary` | Summarise |

### Live events

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/live` | Recent clicks, installs, events, postbacks and rejections for one app |

Polled by the dashboard's live view, and usable directly. Pass the previous
response's `server_time` as `since`. Each poll re-reads the minute before
`since` so an event written late is not lost between polls — so de-duplicate on
`kind` and `id`. Bounded to fifteen minutes; requires `member`.

### Fraud

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/fraud/findings` | List Findings |
| `GET` | `/v1/fraud/installs` | List Flagged Installs |

### Privacy

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/privacy/audit/verify` | Verify Audit Chain |
| `GET` | `/v1/privacy/consent` | List Consent |
| `POST` | `/v1/privacy/consent` | Record Consent |
| `POST` | `/v1/privacy/erasure` | Request Erasure |

### Data export

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/exports/{dataset_name}` | Export Dataset |

### Operations

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Health |
| `GET` | `/ready` | Ready |

## Conventions

**Versioning.** Every path is under `/v1`. See
[API_VERSIONING.md](../API_VERSIONING.md) for what counts as a breaking change.

**Errors.** A JSON body with a `detail` string. `4xx` means the request will
never succeed as sent; `5xx` and `429` are worth retrying.

**Ranges.** Every reporting endpoint takes `since` and `until` and refuses an
unbounded range — an unbounded scan of a partitioned table is a denial of
service that looks like a report. Limits: 90 days for reports, 31 for exports.

**Idempotency.** Events carry a client-minted `event_id` (UUIDv7). Send the same
id twice and it is stored once. This is what makes retrying a timed-out ingest
safe, and it only works if you keep the id across retries rather than minting a
new one.

**Money.** Integer minor units plus an ISO 4217 currency. Never a float.

**Timestamps.** RFC 3339 with an offset. UTC everywhere internally.

68 documented operations across 16 groups.
