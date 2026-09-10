# Device testing

What is verified automatically, what needs real hardware, and what is still
unverified. The last category is the point of this document: it is easy for an
SDK to look finished while nothing has ever run on a phone.

## Automated

| Check | Command | What it proves |
|---|---|---|
| SDK logic | `make sdk` | Queue durability, retry, consent, conversion values — 87 tests |
| iOS compiles | `make sdk-ios` | The native core typechecks against the real iOS SDK |
| **iOS runs** | `make sdk-ios-device` | The core *executes* on a simulator and its invariants hold |
| Android compiles | `make sdk-android` | The Kotlin compiles and lints (see prerequisites below) |
| Native invariants | `pytest tests/test_sdk_native_contract.py` | Static guards on opt-out handling in both languages |
| Cross-language contract | `pytest tests/test_sdk_contract.py` | The SDK's limits and wire fields match the server's |
| A deployment | `uv run python infra/verify/device_checks.py <url>` | Apple's real signed postbacks survive your proxies |

`make sdk-ios-device` earns its place. Typechecking said the iOS core was
correct; running it showed `uname` returning the *host* architecture on a
simulator, so every simulator install reported a device model of `arm64` — a
value that means nothing but looks like data. That bug was invisible to every
other check in the table.

### Prerequisites for `make sdk-android`

- **A JDK 17–21.** Gradle cannot run on JDK 25, which is what Android Studio
  bundles (`Contents/jbr`). Install a 21 and point `JAVA_HOME` at it.
- **Several GB free.** Gradle's caches and the Android build tools are large.

Both are why the Kotlin was not compiled locally for a long time: the machine
this was written on had a JDK 25, which Gradle refuses, and under 1 GB free.

**It compiles in CI now.** The `android` job in `.github/workflows/ci.yml`
builds and lints the module against a real Android SDK on every push, so
"compiles" is no longer an assumption. The target still skips locally when the
toolchain is absent, so `make check` works on a machine without one —
`MMP_REQUIRE_ANDROID=1` turns that skip into a failure, and CI sets it, because
a skip that reports success is how a job stays green while nothing runs.

## Only verifiable on real hardware

Nothing below can be checked by a simulator, an emulator, or CI. Each is a real
failure mode that automated tests cannot reach.

### iOS

**ATT and the IDFA.** Add `NSUserTrackingUsageDescription` to `Info.plist` first
— without it the prompt never appears and authorisation can never be granted.

1. Fresh install. Call `NativeModules.MmpNative.requestTrackingAuthorization()`.
2. **Deny.** `getAdvertisingId()` must return `null` — *not* the all-zero UUID.
3. Delete, reinstall, **allow**. It must return a real UUID.
4. Settings → Privacy → Tracking → turn the app off. It must return `null` again
   without an app restart.

Step 2 is the one that matters. Both platforms answer an opt-out by returning
`00000000-0000-0000-0000-000000000000`, and an integration that passes it along
gives every opted-out device on earth the same identifier — so the server
matches them all to each other.

**SKAdNetwork end to end.** Requires an ad network identifier registered with
Apple and a signed ad; it cannot be simulated. On a simulator
`updatePostbackConversionValue` never calls back at all — the SDK's 3-second
native timeout is what stops that hanging a first launch, and that behaviour was
confirmed by running it, not assumed.

**Backup and restore.** Install, let an `anonymous_id` be minted, back the device
up, restore onto a *second* device. Both must not report the same
`anonymous_id`. If they do, the SDK's storage is not excluded from backup — see
the README. This one is worth doing once and never thinking about again; it is
also the one nobody does.

### Android

**Play Install Referrer.** A sideloaded APK has no referrer. It must be
installed from Play — an internal test track is enough — after a click on a
tracking link, and `getInstallReferrer()` must then return a string containing
the `utm_content` click id.

**The advertising ID.** Settings → Google → Ads → *Delete advertising ID*, then
`getAdvertisingId()` must return `null`.

**Android 13+ and `AD_ID`.** On a device running 13 or later, confirm the merged
manifest still contains `com.google.android.gms.permission.AD_ID`. If a
`tools:node="remove"` anywhere in the app strips it, the identifier reads as all
zeros — indistinguishable from a user opt-out, so attribution stops silently and
nothing errors.

## Verifying a deployment

```bash
uv run python infra/verify/device_checks.py https://track.example.com --api-key pk_...
```

Replays Apple's published postbacks — the real ones, with Apple's real
signatures — at a running host. It is the one check that can catch a TLS
terminator or proxy altering the request body: if anything rewrites it, the
signature stops verifying and the script says so. Run it against staging after
every infrastructure change, not just after a code change.

It sends Apple's own transaction ids, so the postbacks are stored once and
deduplicated forever after.

## Still unverified

Stated plainly, because the rest of this document could otherwise read as
completeness:

- No postback has ever been received from a real device.
- The Kotlin compiles and lints in CI, but has never run on a device.
- No React Native app has been built against this SDK end to end; the JavaScript
  is tested in isolation and the native cores separately.
- The backup/restore case above has not been performed.
