# Integration quickstart

Getting your app measured. Fifteen minutes if nothing is unusual.

## 1. Create an app and a key

```bash
curl -X POST https://api.example.com/v1/apps \
  -H "content-type: application/json" -H "x-csrf-token: $CSRF" -b cookies.txt \
  -d '{"name":"My App","platform":"android","android_package_name":"com.example.app"}'

curl -X POST https://api.example.com/v1/apps/$APP_ID/keys \
  -H "content-type: application/json" -H "x-csrf-token: $CSRF" -b cookies.txt \
  -d '{"name":"production","kind":"client"}'
```

**The raw key is returned once and never again.** Only an HMAC of it is stored.
If you lose it, rotate — there is no reveal, because a key you can retrieve is a
key an attacker can retrieve.

A `client` key ships inside your app binary and is not a secret: it authorises
writing events for one app and nothing else. A `server` key is a secret and is
used with request signing.

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
`signup` or `consent_update` yourself — the SDK owns them.

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
request. This is the path where a client key is not enough.

```python
import hashlib, hmac, json, time, requests

body = json.dumps({"events": [{
    "event_name": "purchase",
    "anonymous_id": "device-abc",
    "event_id": "018f...",          # UUIDv7 you mint and keep across retries
    "revenue_minor": 499,
    "currency": "USD",
}]}).encode()

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

## 6. Receive conversions

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

## 7. Read your data

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
