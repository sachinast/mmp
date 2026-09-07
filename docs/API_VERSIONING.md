# API versioning and deprecation policy

**Status:** binding as of Phase 0. Written before any external endpoint exists,
because the moment an ad network is calling a URL, this policy is no longer a
choice we get to make.

## Scope

Three surfaces carry independent compatibility promises:

| Surface | Versioned by | Consumers |
| --- | --- | --- |
| Tracking + S2S API | URL prefix — `/v1/events`, `/v1/s2s/events` | SDKs, advertiser backends |
| Tracking links | Path shape — `/c/{code}` | Ad networks, end users |
| Postback payloads | Per-rule template, pinned at rule creation | Ad networks, affiliates |
| Dashboard API | Not publicly versioned | Our own dashboard only |

The dashboard API is explicitly *not* a public contract. It changes with the
dashboard. Do not let a customer integrate against it — that is how an internal
API becomes an accidental product.

## What counts as a breaking change

Breaking, requiring a new major version:

- Removing or renaming a field in a request or response.
- Narrowing an accepted value set, or adding a required request field.
- Changing the type or meaning of an existing field.
- Changing a success status code, or the semantics of one (`202` → `200`).
- Removing a postback template variable.
- Tightening validation such that a previously accepted payload is rejected.

Non-breaking, shipped continuously without a version bump:

- Adding an optional request field.
- Adding a response field. **Clients must ignore unknown fields**; this is
  stated in the SDK contract and in the integration docs.
- Adding a new event type, endpoint, or postback variable.
- Performance and error-message changes.

## Deprecation window

1. **Announce.** Email to every organisation with traffic on the affected
   endpoint, plus a dashboard banner.
2. **Signal in-band.** Responses carry `Deprecation: true` and a
   `Sunset: <RFC 1123 date>` header for the entire window.
3. **Wait — minimum 90 days** from announcement to removal. For anything a
   *mobile SDK* calls, the minimum is **180 days**, because app update adoption
   is not under our control and a meaningful tail of installs never updates.
4. **Measure before removing.** Traffic on the deprecated version must be under
   0.1% of requests for 14 consecutive days. If it is not, the window extends;
   it does not expire on schedule regardless of who is still calling.
5. **Remove**, and keep returning `410 Gone` with a link to migration notes for
   a further 90 days rather than a bare `404`.

Two major versions are supported concurrently. Never three.

## SDK versioning

SDKs follow semver, with the major version independent of the API version. An
SDK release states the minimum API version it requires. A breaking SDK change
requires the same 180-day window as a breaking API change, and the same
adoption measurement — a bad SDK release runs inside other people's production
apps, and it cannot be rolled back by us.

## Postback templates

A postback rule pins the template variables available at creation time. Adding
a variable is always safe. Removing one requires notifying the organisations
whose rules reference it, and the rule editor blocks saving a template that
references a variable which no longer exists.
