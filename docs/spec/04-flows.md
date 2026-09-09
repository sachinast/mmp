# 04 — User Flows & UX Specification

**Status:** mixed. The API flows are built and tested. The dashboard covers the
core reporting views only — fraud, SKAdNetwork, deep links and export are
API-only today.

## Advertiser onboarding

```
register → organisation created → create app → create API key (shown once)
        → create campaign → create tracking link → integrate SDK → traffic
```

**The key is shown once.** Registration returns the raw key exactly once and
stores only an HMAC of it. There is no "reveal" — a key you can retrieve is a
key an attacker can retrieve. Rotation issues a new one with an overlap window.

Friction worth keeping: creating an app requires the platform and package
name/bundle id up front, because attribution cannot be repaired later if these
are wrong.

## The click → install → conversion path

This is the product, so it is worth reading as one sequence.

```
1. Person taps a tracking link
     GET /c/{code}?dl=/product/123
     → in-memory link lookup (no database)
     → mint UUIDv7 click_id
     → classify user agent, hash IP
     → 302 to the store, click_id inside the Play referrer (Android)
     → click appended to a buffer, not awaited

2. Store install, first launch
     SDK mints anonymous_id, reads the Play Install Referrer
     → POST /v1/events  {"events":[{"event_name":"install", ...}]}

3. Attribution worker
     load candidate clicks in the window
     → referrer > click_id > device_match > organic
     → fraud assessment inline
     → write one attribution (partial unique index enforces "one install, one row")

4. Later conversion
     POST /v1/events  {"event_name":"purchase","revenue_minor":499,...}
     → resolved to the attribution through the identity cache

5. Postback
     rule matches → template rendered from an allowlist → SSRF-guarded request
     → delivery recorded, retried with backoff
```

Failure at every step degrades rather than breaks: an unknown tracking code
still 404s fast, an unattributable install is recorded as organic, a failed
postback retries.

## Deferred deep link

```
tap link with ?dl=/product/123  →  store  →  install  →  first launch
   → MMP.getDeferredDeepLink()
   → POST /v1/deeplink/resolve {anonymous_id}
   → {"destination":"/product/123","matched":true}  → app navigates
```

The app must not block its first screen on this. It returns `null` rather than
waiting, and an unknown device, an expired window, and a device with no
destination all return the **same** response — otherwise the endpoint is an
oracle for whether an identifier installed an app.

## Consent

```
app shows its own consent UI
   → MMP.setConsent({analytics:"granted", advertising:"denied"})
   → sent as a consent_update event, flushed immediately
   → server records it, then minimises every event in the same batch
```

The SDK does not decide what consent is required, show a dialogue, or interpret
a jurisdiction. It reports what the app tells it. What it *does* enforce is that
the advertising ID is never requested from the platform unless the purpose is
granted — never read, rather than read and withheld.

Consent events are processed **before** other events in the same batch, so an
SDK flushing right after the user accepts behaves as intended.

## Erasure

```
POST /v1/privacy/erasure {scope, identifier}
   → events and attributions deleted
   → clicks keep the row, clear device_hash/ip_hash/user_agent
```

Clicks are cleared rather than deleted deliberately: deleting would change a
click count for a period already billed and already reported. Clearing the
identifier removes the link to a person while leaving the fact that a click
happened.

## Dashboard

Built (`services/web`): overview, campaign performance, event breakdown,
tracking link management.

**Not built:** fraud findings, SKAdNetwork reporting, deep link registry,
export, integration management. All are API-only.

Three bugs in the dashboard were found by *looking at the rendered page* rather
than by any assertion — worth remembering as a testing gap, not just a UX one.

## UX principles applied

- **Fail visibly at configuration time, quietly at runtime.** A bad postback
  template is rejected when saved; a postback that cannot be delivered retries
  silently and appears in a delivery log.
- **Never invent a number.** A withheld SKAdNetwork conversion value is reported
  as `suppressed`, not averaged away.
- **Say what a number cannot be compared with.** The SKAdNetwork summary carries
  its own caveat string in the response body, so it survives a dashboard
  rewrite.
