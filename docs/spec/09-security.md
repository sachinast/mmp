# 09 — Security & Compliance

**Status:** describes the built system. This is a summary; the authoritative
document is [SECURITY.md](../SECURITY.md), which is longer and more specific.

## Posture

Security here is structural rather than added: the controls live where an
application bug cannot bypass them. Row-level security in PostgreSQL rather than
a `WHERE` clause someone must remember. Column privileges rather than a query
that promises not to select a column. Allowlists rather than sanitisers, because
a sanitiser only has to miss one character.

## Controls

| Area | Control |
|---|---|
| Tenant isolation | RLS on 22 tables, `ENABLE` **and** `FORCE` |
| Service privilege | 4 least-privilege database roles |
| Passwords | argon2id |
| Sessions | HttpOnly + Secure + SameSite cookies, CSRF on unsafe methods |
| API keys | HMAC-SHA256 under a KMS-held pepper; raw value never retrievable |
| S2S | HMAC canonical-request signing + nonce replay cache |
| Secrets at rest | AES-256-GCM, KMS-wrapped DEK, AAD bound to the organisation |
| PII | Advertising IDs and IPs hashed at the edge, raw discarded |
| Outbound | SSRF: resolve-then-pin-to-IP, blocked ranges, no redirects |
| Templates | Allowlist substitution — **never** a template engine |
| Deep links | Allowlist path validation, or a pre-registered code |
| SKAdNetwork | ECDSA verification against Apple's published key |
| Audit | Hash-chained, tamper-evident, verifiable via API |
| Dependencies | `pip-audit --strict` in CI |
| Static analysis | bandit + ruff security rules |

## Decisions worth understanding

**Not Jinja for postback templates.** Server-side template injection becomes
remote code execution. Substitution is an allowlist of known variables and
nothing else is interpolated.

**Resolve-then-pin for outbound requests.** Checking a hostname then letting the
HTTP client resolve it again is a DNS-rebinding hole. The address is resolved,
checked against blocked ranges, and the connection pinned to that address.
Redirects are not followed — a redirect is a second request that was never
checked.

**No fingerprinting.** The obvious way to raise match rates. It is what Apple's
rules prohibit for cross-app attribution, and it produces attributions nobody
can defend.

**Hashes are not exported.** `device_hash` and `ip_hash` are derived from an
advertising ID and an IP under a system-wide pepper, which makes them stable
pseudonyms for a person. Exporting one turns an internal attribution key into a
join key someone else can re-identify against.

**Opt-out returns the all-zero UUID, not an error.** Both mobile platforms do
this. Treating it as an identifier gives every opted-out device on earth the
same value, and the server then matches them all to each other. It is rejected
in Kotlin, in Swift, and again in the JavaScript wrapper.

## Privacy and compliance

**Consent** has three purposes — analytics, attribution, advertising — and two
modes. In `strict`, unknown denies. Fields that serve only a denied purpose are
dropped at ingest, so a denied purpose means the field never existed.

**Erasure** deletes events and attributions and clears identifiers on clicks
rather than deleting the rows — deleting would change a click count already
billed and reported. A test requires every table with a person-linked column to
appear in either the erasure list or a documented exclusion list.

**Exports leave the erasure boundary.** Once downloaded, no later deletion
request can reach the file. That is inherent, so it is handled by authority and
evidence: `admin` role, and an audit entry written before a single row streams.

**GDPR/CCPA:** the mechanisms exist (consent, erasure, minimisation, audit,
export). Whether the *deployment* is compliant depends on the DPA, retention
configuration, sub-processors and jurisdiction — none of which is a code
question. Nothing here constitutes legal advice.

## Findings from the build

Recorded because they are the useful part:

- `events` and `clicks` had **no RLS** and `mmp_api` could read them. Found by
  audit, not by a test. Enabling it was measured at no cost.
- Test fixtures connected as the **schema owner**, bypassing RLS — every
  cross-tenant assertion was vacuous until fixed.
- **55% of API keys were unparseable** because `token_urlsafe` emits the
  delimiter character. Fixed with base62 and a round-trip property test.
- `deep_links.code` was **globally unique**, so one advertiser could block every
  other from using "summer".
- Several of my own guard tests **passed for the wrong reason** — a SET LOCAL
  test, a cache-tenant test, an identity-query guard, a provider-leak regex.
  Mutation testing found them.

## Open

1. **Egress network isolation** — the outer layer of SSRF defence is missing.
2. **A named security contact** — `SECURITY.md` still has a placeholder.
3. **No external penetration test.**
4. **No formal threat model document**, though the controls above were each
   chosen against a specific attack.
