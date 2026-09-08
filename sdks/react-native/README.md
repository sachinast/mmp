# @mmp/react-native

Mobile measurement for React Native. Installs, events, revenue, attribution and
consent, against the MMP tracker.

## Install

```sh
npm install @mmp/react-native @react-native-async-storage/async-storage
```

The SDK itself has **no runtime dependencies**. Storage is passed in, because a
measurement library is a guest in someone else's app and every dependency it
drags along is a version conflict waiting to happen.

You also need a CSPRNG. React Native does not ship one:

```sh
npm install react-native-get-random-values
```

```ts
import "react-native-get-random-values"; // must be the first import
```

The SDK refuses to mint identifiers without it rather than falling back to
`Math.random`. A weak `anonymous_id` merges two people's data; a weak `event_id`
lets the server's deduplication discard a real event. Both are silent.

## Use

```ts
import { MMP, adaptKeyValueStore } from "@mmp/react-native";
import AsyncStorage from "@react-native-async-storage/async-storage";

await MMP.initialize(
  { apiKey: "pk_live_...", endpoint: "https://track.example.com" },
  { storage: adaptKeyValueStore(AsyncStorage) },
);

await MMP.track("add_to_cart", { properties: { sku: "A-1" } });
await MMP.track("purchase", { revenueMinor: 499, currency: "USD" });
await MMP.setUserId("user-42");
```

`initialize` sends the install event itself, exactly once per install. Do not
send `install`, `login`, `signup` or `consent_update` yourself — the SDK owns
them and rejects them from `track`.

Money is **minor units as an integer** (cents, pence, paise) with a currency.
Floats are refused: money as a float is how a revenue report stops reconciling.

## Consent

```ts
await MMP.setConsent({
  analytics: "granted",
  attribution: "granted",
  advertising: "denied",
});
```

The SDK does not decide what consent is required, show a dialogue, or interpret
a jurisdiction — it reports what you tell it. What it does enforce is that the
advertising ID is **never requested from the platform** unless the purpose is
granted. A denial means the identifier is never read, which is stronger than
reading it and choosing not to send it.

To hold everything until the user has decided:

```ts
MMP.initialize({ ...config, trackWithoutConsent: false }, deps);
```

## Deferred deep links

```ts
const destination = await MMP.getDeferredDeepLink();
if (destination) navigate(destination);
```

Returns where this install was originally headed, or `null`. It never blocks on
the network for long and never throws — your first screen should not depend on
our uptime.

## Native module

Four things cannot come from JavaScript. All are optional and the SDK degrades
without them; supply a `NativeBridge` to enable them:

| Method | Gives you | Without it |
|---|---|---|
| `getInstallReferrer()` | Play Install Referrer (Android) | Attribution falls back to device matching |
| `getAdvertisingId()` | GAID / IDFA | No device-match attribution |
| `getDeviceInfo()` | OS version, model, app version | Those columns are empty |

`getAdvertisingId` **must** return `null` when the user has opted out — ATT
denied on iOS, or `isLimitAdTrackingEnabled` on Android. The SDK only calls it
when consent allows, but the platform's own opt-out is the authority.

## Two operational notes

**Exclude the SDK's storage from device backups** where the platform allows it
(`NSURLIsExcludedFromBackupKey` on iOS, a backup rule on Android). Restoring a
backup onto a second device otherwise carries the `anonymous_id` with it and
makes two devices look like one. The SDK cannot enforce this from JavaScript.

**Pass real storage.** Without it every launch mints a new anonymous id, so
every launch looks like a new install — inflating the number you are billed on
and breaking attribution. The SDK warns loudly, because otherwise it looks like
it is working.

## Behaviour under failure

- Events are persisted before any send attempt; a crash loses nothing.
- An ambiguous failure keeps the events and retries with jittered exponential
  backoff. Every event carries a client-minted `event_id` and it is never
  regenerated, so the server deduplicates a retry rather than double-counting it.
- A `4xx` is dropped rather than retried forever, and reported through the logger.
- The queue is bounded (default 1,000 events); when full, the oldest go first.
- No public method throws. A measurement SDK that crashes your checkout has done
  more damage than it could ever be worth.

## Development

```sh
npm test          # vitest
npm run typecheck # tsc --noEmit (vitest strips types; this is a separate gate)
npm run build
```

`tests/test_sdk_contract.py`, in the server repo, asserts that this SDK's
limits, reserved event names, consent vocabulary, wire fields and endpoint paths
match what the server actually enforces. Change one side and that test fails.
