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

Eleven pages: overview, **live events**, apps, tracking links, events,
attribution, fraud, SKAdNetwork, deep links, integrations, export.

### Live events

Built for the moment someone integrates: fire an event from a test device and
watch for it, or for the reason it was refused. Polls every two seconds and
shows what was *stored*, which is the whole pipeline rather than just the edge.

Rejections are the part nothing else records. A refused request used to leave
no trace but a metric; the tracker now keeps the last fifty per app for an hour —
the reason, never the payload, and only on the failure path.

The script is the first JavaScript in the dashboard, which meant loosening the
content-security policy from "no script" to `script-src 'self'`: a file this
service serves, never inline, never another host. Event names are
attacker-controlled — the SDK key ships inside the app — so the script renders
feed data only through `textContent`; a test forbids `innerHTML` and its
relatives, and an event named `<img onerror=...>` was verified in a browser to
appear as text.

Two bugs were caught before shipping, both found by reading the design rather
than by a test failing. Events are timestamped when received but visible only
once written, so a strict `since` cursor could drop an event forever between two
polls — each poll now re-reads the previous minute. And pausing during an
in-flight request let its response set the status back to "Live".

Apps, campaigns, tracking links and API keys are all created from the
dashboard. Until recently none of them were: every create flow lived only in
the API, so the honest instruction for a new customer was "run these curl
commands", which is not a product.

Connecting a network is a form too, rendered from the adapter's own declaration
of what it needs — so adding an adapter adds its form, and nothing in the
dashboard knows a single field name. Fields marked secret become
envelope-encrypted credentials; everything else is plain configuration.

The key form renders its result rather than redirecting. The raw key exists
exactly once — only an HMAC is stored — so a redirect would put a live
credential in the address bar, the browser history, and every access log
between the server and the user.

Five bugs have now been found by *looking at the rendered page* rather than by
any assertion. The most recent: export links pointed straight at the API, which
works in development — both services answer on 127.0.0.1 and cookies ignore the
port — and would have 401'd in production, where the session cookie is scoped to
the dashboard's host. Downloads are proxied through the dashboard instead.

That is five bugs no test caught, which is the argument for opening the page.

## UX principles applied

- **Fail visibly at configuration time, quietly at runtime.** A bad postback
  template is rejected when saved; a postback that cannot be delivered retries
  silently and appears in a delivery log.
- **Never invent a number.** A withheld SKAdNetwork conversion value is reported
  as `suppressed`, not averaged away.
- **Say what a number cannot be compared with.** The SKAdNetwork summary carries
  its own caveat string in the response body, so it survives a dashboard
  rewrite.
