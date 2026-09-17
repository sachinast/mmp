# Integration quickstart

Getting your app measured. Fifteen minutes if nothing is unusual.

## 1. Create an app and a key

```bash
curl -X POST https://api.example.com/v1/apps \
  -H "content-type: application/json" -H "x-csrf-token: $CSRF" -b cookies.txt \
  -d '{"name":"My App","platform":"android","android_package_name":"com.example.app"}'

curl -X POST https://api.example.com/v1/apps/$APP_ID/keys \
  -H "content-type: application/json" -H "x-csrf-token: $CSRF" -b cookies.txt \
  -d '{"name":"production","kind":"sdk","environment":"prod"}'
```

**The raw key is returned once and never again.** Only an HMAC of it is stored.
If you lose it, rotate — there is no reveal, because a key you can retrieve is a
key an attacker can retrieve.

`kind` is `sdk` or `s2s`. An `sdk` key ships inside your app binary and is not
a secret: it authorises writing events for one app and nothing else. An `s2s`
key *is* a secret and is used with request signing below.

The raw key comes back in `api_key`, alongside a `warning` telling you it will
not be shown again.

## 2. Add the SDK

```bash
npm install @mmp/react-native @react-native-async-storage/async-storage \
            react-native-get-random-values
```

```ts
import "react-native-get-random-values"; // must be the first import
import { NativeModules } from "react-native";
import { MMP, adaptKeyValueStore, createNativeBridge } from "@mmp/react-native";
import AsyncStorage from "@react-native-async-storage/async-storage";

await MMP.initialize(
  { apiKey: "pk_live_...", endpoint: "https://track.example.com" },
  {
    storage: adaptKeyValueStore(AsyncStorage),
    native: createNativeBridge(NativeModules.MmpNative),
  },
);
```

Two mistakes worth avoiding, because both look like the SDK working:

- **No storage adapter.** Every launch mints a new device id, so every launch
  looks like a new install — inflating the number you are billed on and breaking
  attribution. The SDK warns loudly.
- **No CSPRNG polyfill.** The SDK refuses to start rather than fall back to
  `Math.random`. A weak device id merges two people's data.

`initialize` sends the install event itself. Do not send `install`, `login`,
`signup` or `consent_update` yourself — the SDK owns them, and it refuses them
from `track()` however you capitalise or punctuate them.

That matters for naming your own events. Reserved names are matched on a folded
form — lowercased, separators removed — so `Install`, `Sign-Up` and `sign up`
are all the reserved name. If your analytics plan lists an event called
`Sign-Up`, the SDK is already sending it; call `MMP.setUserId(...)` instead and
it is handled for you.

Anything that does not fold to a reserved name is yours: `first_open`,
`Checkout Started`, `mining_started`, `withdrawal_requested` and
`ad_failed_to_load` all work as written, including the space and the
capitals. Names are stored exactly as you send them.

## 3. Track events

```ts
await MMP.track("add_to_cart", { properties: { sku: "A-1" } });
await MMP.track("purchase", { revenueMinor: 499, currency: "USD" });
await MMP.setUserId("user-42");
```

Money is **integer minor units** — cents, pence, paise — with a currency. Floats
are refused: money as a float is how a revenue report stops reconciling.

## 4. Or integrate server to server

For purchases you validate on your own backend, skip the SDK and sign the
request. This is the path an `s2s` key exists for.

```python
import hashlib, hmac, json, time, requests

body = json.dumps(
    {
        "events": [
            {
                "event_name": "purchase",
                "anonymous_id": "device-abc",
                "event_id": "018f...",  # UUIDv7 you mint and keep across retries
                "revenue_minor": 499,
                "currency": "USD",
            }
        ]
    }
).encode()

timestamp = str(int(time.time()))
digest = hashlib.sha256(body).hexdigest()
canonical = f"POST\n/v1/s2s/events\n{timestamp}\n{digest}"
signature = hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()

requests.post(
    "https://track.example.com/v1/s2s/events",
    data=body,
    headers={
        "authorization": f"Bearer {API_KEY}",
        "content-type": "application/json",
        "x-mmp-timestamp": timestamp,
        "x-mmp-signature": signature,
    },
)
```

The signature covers the method, path, timestamp and a digest of the body, so a
replay with an altered body is refused. Timestamps outside the window are
rejected and nonces are cached against replay.

## 5. Create a tracking link

```bash
curl -X POST https://api.example.com/v1/tracking-links \
  -H "content-type: application/json" -H "x-csrf-token: $CSRF" -b cookies.txt \
  -d '{"campaign_id":"...","name":"summer","fallback_url":"https://example.com"}'
```

Give the returned `https://track.example.com/c/{tracking_code}` to the network.
Add `?dl=/product/123` for a deep link destination, or `?dl_code=summer` to name
one you registered.

## 6. Share links with partners

Create a campaign per partner on **Tracking links**, then give the partner its
link with their own click id appended:

```
https://<your tracking domain>/c/<code>?sub1={their_click_id}&sub2={their_publisher_id}
```

The partner replaces the braces with their own macros. `sub1`, `sub2` and `sub3`
are stored on the install that click earns, so they are still there when a
purchase arrives days later.

On **Postbacks**, create a rule for that partner's campaign and return their
click id with `{{sub1}}`:

```
https://partner.example/postback?clickid={{sub1}}&event={{event_name}}&payout={{revenue}}
```

`{{sub1}}`–`{{sub3}}` are refused in a rule for every campaign: they carry one
partner's click ids, and an app-wide rule fires for every partner's installs.
Watch deliveries arrive on **Live events**.

## 7. Receive conversions

**Postbacks** push to a network's URL when a rule matches. **Webhooks** push to
*your* endpoint.

Both are at-least-once. **Make your receiver idempotent** — deduplicate on the
delivery id. A receiver that double-counts a retry will double-count under any
network partition, and partitions are not rare.

Verify a webhook before trusting it:

```python
expected = hmac.new(SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
if not hmac.compare_digest(expected, request.headers["x-mmp-signature"]):
    return 401
```

Use `compare_digest`, not `==`.

## 8. Check it arrived

Open **Live events** in the dashboard before you send anything. Clicks, installs,
events and postback deliveries appear within a few seconds of being stored —
stored, not merely received, so an event there has been through the whole
pipeline.

If something never appears, look for a red **rejected** row. The tracker keeps
the last fifty refusals per app for an hour, with the reason: a revenue amount
with no currency, malformed JSON, an SDK key sent to the server-to-server
endpoint, a signature that did not verify. One invalid event refuses the whole
request, and the row says how many went with it.

## 9. Read your data

```bash
curl -G https://api.example.com/v1/analytics/overview \
  --data-urlencode "app_id=$APP_ID" \
  --data-urlencode "since=2026-09-01T00:00:00Z" \
  --data-urlencode "until=2026-09-08T00:00:00Z" -b cookies.txt

curl -G "https://api.example.com/v1/exports/events" \
  --data-urlencode "app_id=$APP_ID" ... -b cookies.txt -o events.csv
```

Ranges are bounded — 90 days for reports, 31 for exports. Exports stream CSV and
require the `admin` role, because taking the dataset out is a different act from
reading a report.

## Retrying safely

Retry `5xx`, `429` and network failures. Do **not** retry `4xx` — the server
understood and refused, and retrying says the same thing.

Keep the `event_id` identical across retries. That is what lets the server
deduplicate rather than double-count a purchase. Minting a fresh id on retry is
easy to do by accident and turns one sale into two.

Back off exponentially **with jitter**. Without it, every client that was
offline during an outage returns at the same moment and knocks the recovering
server over again.

## Consent

```ts
await MMP.setConsent({ analytics: "granted", attribution: "granted", advertising: "denied" });
```

The platform enforces this at ingest: a field that only serves a denied purpose
is dropped before storage, so a denied purpose means the field never existed.
Set your app's mode to `strict` if consent must precede processing — then
"unknown" denies rather than allows.
