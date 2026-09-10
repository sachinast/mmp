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

Three things cannot come from JavaScript. All are optional and the SDK degrades
without them.

| Method | Gives you | Without it |
|---|---|---|
| `getInstallReferrer()` | Play Install Referrer (Android) | Attribution falls back to device matching |
| `getAdvertisingId()` | GAID / IDFA | No device-match attribution |
| `getDeviceInfo()` | OS version, model, app version | Those columns are empty |

Implementations ship in `ios/` and `android/`. Wire them up:

```ts
import { NativeModules } from "react-native";
import { MMP, createNativeBridge, adaptKeyValueStore } from "@mmp/react-native";

await MMP.initialize(config, {
  storage: adaptKeyValueStore(AsyncStorage),
  native: createNativeBridge(NativeModules.MmpNative),
});
```

`createNativeBridge(undefined)` is a supported state — it returns a bridge that
answers "not available" to everything, which is what a JavaScript-only
integration looks like.

### iOS

Add to `Info.plist`, or ATT can never be granted:

```xml
<key>NSUserTrackingUsageDescription</key>
<string>Explain, in your own words, what the identifier is used for.</string>
```

**The SDK never presents the ATT prompt.** It can be shown once per install and
your app owns that moment — after explaining why, somewhere it makes sense. An
SDK that fires it during `initialize` spends your one chance on a cold launch.
Call it yourself when you are ready:

```ts
await NativeModules.MmpNative.requestTrackingAuthorization();
```

Until it is granted the IDFA is never read at all — not read and withheld.

### Android

Autolinking picks up `MmpPackage`. Play Services and the referrer library are
`compileOnly` here, so **your app adds whichever it wants**:

```gradle
implementation "com.google.android.gms:play-services-ads-identifier:18.0.1"
implementation "com.android.installreferrer:installreferrer:2.2"
```

They are not forced on you deliberately: an SDK that pins its own Play Services
version breaks somebody's release, and adding these is also you deciding that
you want that data collected. The SDK handles their absence.

The `com.google.android.gms.permission.AD_ID` permission is declared by this
package and is required from Android 13. If you strip it with
`tools:node="remove"`, the advertising ID reads as all zeros — which looks
exactly like a user opt-out, so attribution stops silently.

### On opt-outs

Both platforms answer "the user said no" by returning the all-zero UUID rather
than by failing. The SDK treats that, an empty string, and literal `"null"` as
no identifier. If you write your own bridge instead of using
`createNativeBridge`, you must do the same — otherwise every opted-out device
sends the same value and the server matches them all to each other.

### Verification status

- The JavaScript layer is unit-tested (`npm test`).
- The iOS core is typechecked against the real iOS SDK (`make sdk-ios`) **and
  executed on a simulator** (`make sdk-ios-device`). Running it is what found
  that `uname` returns the host architecture on a simulator, so every simulator
  install was reporting a device model of `arm64`.
- The Kotlin **compiles and lints in CI** on every push, against a real Android
  SDK (`make sdk-android`). Locally it needs a JDK 17–21; the target skips
  without one so `make check` still runs.
- Neither native half has run on real hardware.

`docs/DEVICE_TESTING.md` lists what only a real phone can verify, and why each
one matters.

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
