# SKAdNetwork

Apple's attribution framework for iOS. It replaces the device-level matching the
rest of this platform uses, and it is a different measurement model rather than
a degraded version of the same one — treating it as the latter is how a
dashboard ends up with two install counts that never reconcile and nobody can
explain.

## What actually arrives

One signed postback per install, per network, delayed by up to several days.
It carries the campaign identifier, whether this network won, and — sometimes —
six bits about what the user did afterwards. That is all. No event stream, no
revenue figure, no session count.

Apple withholds parts of it below privacy thresholds: the campaign identifier
loses precision, and the conversion value can be absent entirely. A postback
with a null conversion value is normal, not an error.

## Verification

`packages/mmp_attrib/src/mmp_attrib/skadnetwork.py`.

Postbacks arrive at a public URL with no credential. **The ECDSA signature is
the entire authentication.** Without it, the endpoint is an anonymous way to
manufacture installs and claim credit for organic ones.

Apple signs a UTF-8 string built by joining specific parameters, in a
version-specific order, with `U+2063` INVISIBLE SEPARATOR. The orders are in
`FIELD_ORDER`, and `tests/test_skadnetwork.py` checks them against **five
postbacks Apple published together with their real signatures** — 2.2, 3.0
winning, 3.0 non-winning, and two 4.0 web postbacks. That is conformance to
Apple's signing behaviour, not to a reading of their prose. It matters because a
subtly wrong order rejects every genuine postback, and the tempting fix for
"nothing verifies" is to stop verifying.

Supported: 2.1, 2.2, 3.0, 4.0. Versions 1.0 and 2.0 are **refused** — they use
keys Apple issues through the registration portal rather than the published one,
and a postback we cannot verify is one we cannot count.

### Two things the signature does not cover

**The conversion value.** Neither `conversion-value` nor
`coarse-conversion-value` is signed, in any version. A genuine postback can be
replayed with a different value and still verify. The global uniqueness of
`transaction-id` limits the damage — the first value recorded is the one that
stands — but downstream code must treat the value as *reported*, never as
proven. There is a test asserting this so the property stays known.

**Nothing prevents a network from simply not telling you.** Postbacks go to the
ad network's URL; the developer copy is opt-in via `NSAdvertisingAttributionReportEndpoint`.
If you only receive the network's forwarded copy, you are trusting the network.

## The endpoint

`POST /.well-known/skadnetwork/report-attribution` on the tracker. The only
unauthenticated write path in the platform, so:

- Verify before any database work or logging. Otherwise an anonymous caller can
  make us do work, and write strings of their choosing into our logs, for free.
- One identical response for stored, duplicate, and unknown-app. A distinguishable
  answer would enumerate our customers' App Store ids to anyone.
- Answer `200` to anything genuinely from Apple, even when unusable — Apple
  retries for nine days without one, and those retries would fail identically.
- The body size is capped; an anonymous caller does not choose how much we read.

Duplicates are ordinary operation, not attack traffic: the retry behaviour above
means the same postback legitimately arrives more than once.

## Conversion values

Six bits — 0 to 63 — set by the app before Apple's timer expires. Deciding what
those 64 values mean is the advertiser's modelling decision, so the mapping is
configuration (`PUT /v1/skan/conversion-values`), not code.

Two rules the SDK enforces, because getting either wrong is silent:

- **Apple ignores a decrease.** It is not an error and not a rollback; the call
  simply does nothing. An SDK that maps events to values and calls on every
  event appears to work while discarding most of what it sends.
- **Every accepted call restarts the measurement window.** A chatty app delays
  its own postback.

`sdks/react-native/src/conversion.ts` tracks the high-water mark and only calls
on a genuine increase, persisting it so a relaunch does not re-send.

## Reporting

`GET /v1/skan/summary` groups by network and campaign and reports `suppressed` —
the count of winning postbacks whose conversion value Apple withheld. An average
computed over an unstated fraction of the data is exactly the number that gets
quoted without its denominator.

The response carries its own caveat string, deliberately: the warning travels
with the data rather than living in a dashboard someone may reimplement.

**Do not add SKAdNetwork installs to deterministic installs.** They are delayed,
privacy-thresholded, and a non-winning postback means another network was
credited rather than that nothing happened.

## Not done

- **Registration with Apple.** Receiving postbacks as an ad network requires an
  SKAdNetwork identifier registered with Apple. That is a business process, not
  code, and nothing here substitutes for it.
- **Ad signing.** Serving StoreKit-rendered ads requires signing impressions
  with a private key registered with Apple. This platform receives postbacks; it
  does not sign ads.
- **Conversion-value modelling tools.** Advertisers get a raw mapping table. The
  hard part — choosing an encoding that fits revenue, funnel stage and time into
  six bits — is left to them, with no tooling.
- **Multiple conversion windows.** SKAdNetwork 4 sends up to three postbacks per
  install with a `postback-sequence-index`. They are stored and reported
  individually; nothing stitches them into one view of a user's progression.
- **No postback has been received from a real device.** Verification is proven
  against Apple's published vectors, and the endpoint against synthetic ones.
  An end-to-end test with a real device and a registered network identifier is
  outstanding.
