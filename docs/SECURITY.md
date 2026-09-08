# Security

What this platform does to protect the data it holds, what it deliberately does
not do, and what is still open. Written after an audit of the code as it stands,
not as an aspiration — where something is unfinished it says so.

Every control described here has a test that fails if it is removed. Where a
control is enforced only by convention, that is stated.

---

## The three risks that shape the design

Most of the security work in this codebase serves one of three concerns. They are
listed in the order a compromise would hurt.

1. **One advertiser reading another's data.** Multi-tenancy is the whole product.
2. **Stored partner credentials leaking.** We hold API tokens for ad networks on
   customers' behalf.
3. **The postback engine being turned into a request forwarder.** We fetch
   user-supplied URLs from inside our own network.

---

## 1. Tenant isolation

Two independent layers. The application scopes every query to the active
organisation; Postgres row-level security enforces the same rule underneath. A
forgotten `WHERE organization_id` is then a bug that returns no rows, not a
breach.

- Every table carrying `organization_id` has `ENABLE` **and** `FORCE ROW LEVEL
  SECURITY`. Without `FORCE`, the table owner bypasses the policy silently.
- Policies fail closed: an unset tenant matches nothing, never everything.
- Four roles, none owning a table, none holding `SUPERUSER` or `BYPASSRLS`:

  | Role | May do |
  | --- | --- |
  | `mmp_api` | DML on business tables, tenant-scoped. No DDL. |
  | `mmp_tracker` | INSERT on `events` and `clicks` only; SELECT on `api_keys`, `apps`, `tracking_links`. Cannot read events back. |
  | `mmp_worker` | Crosses tenants by design, via a **named policy** visible in the catalogue rather than a role attribute. |
  | `mmp_readonly` | SELECT, for BI tools. |

**The `SET LOCAL` requirement.** Under PgBouncer in transaction pooling mode a
connection returns to the pool the instant a transaction commits. A tenant set
with plain `SET` survives that handoff and applies to the next request, on behalf
of a different organisation — a cross-tenant read with no bug in any query.
`Database.tenant_connection` is the only supported request-path accessor and
always uses `SET LOCAL` inside an explicit transaction.

> Found during the audit: `events` and `clicks` carried `organization_id`, had no
> RLS, and were readable by `mmp_api`, which holds SELECT on every table. Nothing
> queried them from the API — the dashboard reads rollups — so no test failed.
> The event explorer would have been the first query to cross a tenant. RLS was
> measured at no cost on the ingest path (13.2 ms → 12.0 ms per 5,000-row batch)
> and enabled.

### Known exception: identity tables

`users` and `organization_members` have **no RLS**, deliberately. They are read
*before* a tenant is established — resolving which organisation a session belongs
to is what the membership lookup is for — so a policy keyed on the active
organisation would make it impossible to determine the active organisation.

The exception is safe only while every query against them filters explicitly.
That property is enforced by `tests/test_identity_queries.py`, which walks the
API source and fails on any statement touching those tables without a `user_id`,
`organization_id`, `email` or primary-key filter.

**This is weaker than RLS and should be treated as such.** It catches the
realistic mistake — a new endpoint that forgets a filter — and would not catch a
deliberately crafted one. Closing it properly would mean threading the
authenticated user id into a session setting and writing a policy over both
columns; that is worth doing and is not done.

---

## 2. Credentials

| Secret | At rest | Why |
| --- | --- | --- |
| User password | argon2id (19 MiB, t=2) | Verified once per login behind a rate limiter. Slow is the feature. |
| App API key | HMAC-SHA256 under a KMS pepper | Verified on **every ingest request**. A slow hash here would be a self-inflicted denial of service; 256 bits of entropy does the work instead. Prefix stored plaintext and indexed so authentication is one lookup and one constant-time compare. |
| S2S signing secret | Derived from the key hash under the pepper | Both sides can compute it; nothing extra to store or rotate. |
| Partner credentials | AES-256-GCM, KMS-wrapped DEK | Must be recoverable. AAD binds the ciphertext to its organisation, so a row copied to another tenant fails to decrypt rather than quietly working. |
| Webhook signing secret | Same envelope scheme | The one credential that cannot be hashed — we must reproduce it to sign. Returned once at creation. |
| Session | Opaque token in Redis | Revocable. A JWT would make logout a lie. |

- Raw API keys are returned exactly once. `GeneratedKey.__repr__` redacts itself
  so a traceback cannot spill one.
- Postback header values are write-only: encrypted at rest, only names returned.
- A test asserts the raw key never reaches the database, a log line, or a repr.

**KMS.** `mmp_crypto.kms` wraps data keys with a customer master key held in
KMS. `seal` generates the data key locally and asks KMS only to wrap it, so the
plaintext key never travels to AWS and the local development provider remains a
faithful stand-in rather than a different code path.

Unwrapped data keys are cached in memory, bounded by size and age. That is a real
trade-off: a revoked grant takes up to the TTL to take effect, and plaintext data
keys sit in process memory for that long. `clear_cache()` exists so an incident
response does not require a restart. The alternative — a KMS call per webhook
delivery — would add tens of milliseconds and a per-request bill to the outbound
path.

Production **requires** `MMP_KMS_KEY_ID`. The check lives where the provider is
constructed, not in a checklist: a control that depends on someone remembering is
not a control.

**Untested against real AWS.** The KMS tests use a stand-in client and prove this
module's own logic — caching, encryption context, version handling, error
containment. They prove nothing about whether the boto3 call shape is right. The
first real deployment is the first time that is tested.

---

## 3. Outbound requests (SSRF)

Postback rules and webhooks are user-supplied URLs that our servers fetch. This
is the sharpest surface in the platform: unguarded, anyone who can create a rule
has a request forwarder inside the VPC, pointed above all at `169.254.169.254`,
which on a misconfigured instance hands out credentials.

`mmp_core.outbound` does, in order:

1. Require `https`.
2. Resolve the hostname **itself**, once.
3. Reject **every** returned address in a private, loopback, link-local,
   multicast, reserved or metadata range — every address, because a name can
   resolve to one public and one private one.
4. Connect to the **validated IP**, carrying the original `Host` header and TLS
   server name.
5. Refuse redirects entirely.

Step 4 is what defeats DNS rebinding, and it is the step that is easy to get
wrong: validating a hostname and then handing the same hostname to the HTTP
client means two lookups, and an attacker only has to make the second answer
differently. A test asserts the connection goes to the pinned address.

Destinations are validated **when a rule is saved**, so a bad one is a 422 the
user sees rather than a delivery failure found in a log. Patching a rule out of
sandbox revalidates, or the check could be skipped by creating in sandbox mode.

**Defence in depth, not sole defence:** outbound delivery should also run from a
network segment with no route to internal services. That control lives in
infrastructure and is listed in the production checklist.

---

## 4. Template injection

Postback URLs contain `{{click_id}}`-style placeholders. Rendering those with
Jinja — already a dependency — would hand anyone who can create a rule
server-side template injection and, through the usual `__class__` /
`__subclasses__` chain, code execution on a worker holding database credentials.

`mmp_providers.templates` is **not a template engine**. It is a fixed allowlist
of variable names substituted from a dict, values URL-encoded, with no expression
evaluation and no attribute access. Any `{{...}}` that is not a well-formed,
allowlisted placeholder is rejected when the rule is saved.

Tested against the standard SSTI corpus; a structural test fails if `jinja`,
`Template(`, `eval(` or `exec(` ever appears in that module.

---

## 5. Request authenticity

**Inbound S2S.** Signed over a canonical request — scheme version, method, path,
timestamp, body digest. Signing only the body would allow replay against a
different endpoint; signing only a timestamp would allow the payload to change.
Replay protection is timestamp **and** nonce: a timestamp alone permits replay
for its whole tolerance, a nonce cache alone would be unbounded.

**Outbound webhooks.** The same canonical form in the other direction, so a
customer implementing verification follows one description rather than two.
The verification snippet ships with the secret and lives beside the signing code,
because a documented scheme that drifts from the implementation silently rejects
everything.

Every comparison uses `hmac.compare_digest`. Failures return one message
regardless of cause — distinguishing "bad signature" from "stale timestamp" tells
an attacker which half of their forgery to work on.

---

## 6. Input handling

- Payloads capped at 256 KB, enforced **while reading** rather than by trusting
  `Content-Length`.
- Compressed bodies capped at 4 MB decompressed. Without it, a few hundred
  kilobytes of gzip is a memory-exhaustion primitive for an unauthenticated
  caller.
- Event names capped at 120 characters: an unbounded name from a misconfigured
  SDK would explode rollup cardinality, which is very hard to undo.
- Postback headers reject `Host`, `Content-Length`, `Transfer-Encoding` and any
  value containing a newline. The first would defeat destination pinning; the
  last is request splitting.
- Tracking codes are 22 base62 characters (~131 bits). A code appears in ad
  creative and browser history; a short or sequential one lets a competitor
  enumerate an advertiser's campaign structure.

---

## 7. Consent

Consent is evaluated **at the edge, before anything is stored**. Checked after
persistence it becomes a deletion problem: the data is already in a partition, a
rollup, a postback and a partner's system. Checked at ingest, a denied purpose
means the field never existed.

Purposes are separable — analytics, attribution, advertising — because a user may
allow us to count that an install happened and refuse to have it attributed to an
ad network. Forwarding to a third party requires *both* attribution and
advertising: sending a conversion to a network is an advertising use of an
attribution, and someone who allowed one but not the other has not agreed to it.

**An explicit denial is always honoured**, in any configuration. What "unknown"
means is per-app and explicit:

| Mode | Unknown consent |
| --- | --- |
| `permissive` (default) | proceeds |
| `strict` | denies |

The default is permissive **not** because it is safer — it is not — but because
strict-by-default would silently stop attributing every existing advertiser's
installs the moment this shipped, which is a data loss event wearing a privacy
feature's clothes. Strict is one field away and is the correct setting for an app
serving users in a consent jurisdiction. Making it the default is a commercial
decision and belongs to whoever owns that.

## 8. Erasure

A deletion request is the one privacy operation that cannot be partially done.
Erasing events but leaving attributions produces a system that reports having
deleted data it still holds — worse than not deleting, because it is a claim.

`mmp_db.erasure` enumerates every table explicitly, and a test walks the schema:
a new table carrying an `anonymous_id` fails until someone has decided what
erasure means for it.

Two deliberate exclusions, both stated in the code:

- **Aggregate rollups** are left alone. A row saying "412 installs this hour"
  contains no identifier, and recomputing history to subtract one person would
  corrupt reporting an advertiser has already acted on while achieving nothing
  for the individual.
- **Clicks are de-identified rather than deleted.** Deleting one would change a
  click count for a period already billed and already reported; clearing the
  device hash, IP hash and user agent removes the link to a person while leaving
  the fact that a click happened.

Requests and completions are both written to the audit log, so a partial failure
is visible rather than reported as success.

## 9. The audit log

Hash-chained: each entry carries the hash of its predecessor, so editing one
breaks the chain for everything after it. `/v1/privacy/audit/verify` walks it.

**Tamper-evident, not tamper-proof**, and the API says so in every response.
Anyone who can write to the table can rewrite the chain from the point they
altered; detecting that needs the chain head recorded somewhere they cannot
reach, which is a deployment concern and is not done. Concurrent writers can also
fork the chain — edits are still detected, but the ordering is not total.
Serialising every audit write would put a lock on every mutation in the platform.

A log described as tamper-proof when it is only tamper-evident is worse than a
plain log, because someone will rely on it.

## 10. Privacy

- **Raw IP addresses are never stored.** Hashed with HMAC under a pepper that
  rotates **daily**, which caps correlation to a single day — including for us.
  Rotation is the difference between a hashed identifier and a pseudonymous one
  that a rainbow table over 4 billion IPv4 addresses re-identifies instantly.
- **Advertising IDs are hashed at the edge** and the raw values stripped from the
  payload before it is queued, so they exist only in the tracker process for one
  request.
- **The all-zero advertising ID is not hashed.** Android returns it for a user
  who opted out; hashing it would create one bucket matching every opted-out
  device to every other one.
- **No fingerprinting.** No IP-plus-device-model matching, at any tier. It would
  raise the match rate, is what Apple's rules prohibit for cross-app attribution,
  and produces attributions that cannot be defended when an advertiser asks how a
  number was derived.
- **Logs are PII-free** by a redaction processor over a named key set, with a
  test asserting the set stays complete.

---

## 8. Enforced repository rules

Conventions do not survive contact with a deadline. These are tests:

| Rule | Enforced by |
| --- | --- |
| No f-string SQL outside three reviewed modules in `mmp_db` | AST walk over every file |
| No `pickle`/`dill`/`shelve` import anywhere | AST walk; queue payloads are JSON or msgpack |
| No PII in logs | Redaction processor plus a key-set assertion |
| No placeholder secrets | Settings validator rejects `change…`, `todo…` |
| Migrations never read live metadata | Import guard plus a full chain applied to an empty database |
| Every identity query is filtered | AST walk over the API source |
| Tenant tables all have RLS | Metadata walk plus a catalogue check |

CI additionally runs `ruff`, `mypy --strict`, `bandit` and `pip-audit` against a
hash-pinned lockfile.

---

## What is not done

Stated plainly, because a security document that only lists strengths is
marketing.

- **KMS is untested against real AWS.** The integration is implemented and
  covered against a stand-in client; the call shape is unverified.
- **Identity tables have no RLS**, mitigated by a static check (§1).
- **Consent is not resolved from the database on the ingest path.** The tracker
  reads a Redis cache the SDK populates; a device whose consent was recorded
  through the API but which has not reported since is treated as unknown until
  the cache is warmed. Acceptable under `permissive`, a real gap under `strict`.
- **No bulk erasure.** One device at a time, synchronously. Erasing a whole app
  or organisation needs the asynchronous path and does not exist.
- **The audit log records configuration changes only.** Reads are not recorded.
- **No penetration test.** Everything here is self-assessed.
- **No rate limiting on the business API.** Login is limited per account and per
  address; the rest of the API is not. An authenticated user can currently make
  unlimited requests.
- **No audit log for data access.** `audit_log` exists and is hash-chained, but
  only configuration changes are written to it. Reads are not recorded.
- **No secret rotation schedule.** Rotation is implemented for API keys and
  webhook secrets; nothing enforces or reminds.
- **The published latency SLO is unverified.** It needs a load generator run from
  separate hardware; the in-repo benchmark is a regression gate and says so.

---

## Reporting

Security issues should go to the platform team directly rather than through a
public issue. This section needs a real address before the platform is exposed to
anyone outside the company.
