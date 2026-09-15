# 03 — User Roles & RBAC

**Status:** describes the built system. Roles extracted from
`services/api/src/mmp_api/deps.py` and endpoint requirements counted from the
route modules on 2026-09-09.

## Two independent layers

Authorisation here is **not** one mechanism. There are two, and they fail
differently on purpose.

**Application roles** decide what a *user* may do. Checked per request.

**Database roles + row-level security** decide what a *service* may reach. A
compromised API process still cannot read another tenant's rows, because the
constraint is in PostgreSQL rather than in the code that was compromised.

A control that lives only in application code is one an application bug can
bypass. That is why both exist.

## Application roles

Ranked, in `ROLE_RANK`:

| Role | Rank | Intent |
|---|---|---|
| `viewer` | 0 | Read reports. Cannot change anything. |
| `member` | 1 | Day-to-day work: campaigns, links, reading fraud findings. |
| `admin` | 2 | Configuration and anything that leaves the system. |
| `owner` | 3 | Organisation-level control, including membership. |

Checks are `at_least`, not equality — `require_role("member")` admits admins and
owners.

Current distribution across endpoints: **27 admin, 19 viewer, 11 member,
1 owner**.

### Where the lines are drawn

The judgment calls worth knowing:

- **Export requires `admin`, not `member`.** Taking the whole dataset out of the
  system is a different act from reading a report, and it leaves the erasure
  boundary permanently.
- **Integration listing requires `admin`.** The list of networks an advertiser
  works with is commercially sensitive even without the credentials.
- **Fraud findings are `member`.** They are operational information the people
  running campaigns need.
- **Live events are `member`.** Raw per-device events with their properties are
  operational data for people integrating, not a report; a viewer reads
  aggregates. The feed also checks app ownership explicitly, because its
  rejections come from Redis, which row-level security does not cover.
- **Deep link creation is `admin`**, reading is `member`. A destination is
  opened by an app on someone's phone.

### Two rules enforced beyond rank

- **No privilege escalation.** An admin cannot grant a role above their own.
  Test: `test_admin_cannot_grant_a_role_above_their_own`.
- **Authorisation is re-derived per request**, never cached in the session, so
  revoking membership takes effect on the next request rather than at the next
  login. Test: `test_revoked_membership_takes_effect_on_the_next_request`.

## Database roles

Four, least-privilege, one per service:

| Role | Privileges | Notes |
|---|---|---|
| `mmp_api` | SELECT/INSERT/UPDATE/DELETE on tenant tables | Always runs under a tenant-scoped transaction |
| `mmp_tracker` | Mostly INSERT-only | Write-only on `events`, `clicks`, `skadnetwork_postbacks` |
| `mmp_worker` | Full, crosses tenants | The only role that legitimately spans tenants |
| `mmp_readonly` | SELECT | Analysts, debugging |

The tracker is the most exposed service, so its grants are the narrowest. Two
examples of that being taken seriously:

- It can read `attributions` only through **column privileges** (no `click_id`,
  `user_id`, campaign or fraud verdict) **and** an RLS policy limiting it to the
  last 48 hours — for the deferred deep-link handshake.
- It has INSERT but not SELECT on `skadnetwork_postbacks`. That ruled out both
  `RETURNING` and `ON CONFLICT` with a target, since PostgreSQL needs SELECT for
  each; the duplicate is caught by constraint name instead.

## Row-level security

**22 tables**, all `ENABLE` *and* `FORCE`. Without `FORCE`, the table owner
bypasses its own policies.

Tenancy comes from `SET LOCAL mmp.org_id` inside the transaction, which is what
makes it safe under PgBouncer transaction pooling.

```sql
CREATE POLICY org_isolation ON <table>
    USING (organization_id = NULLIF(current_setting('mmp.org_id', true), '')::uuid)
    WITH CHECK (organization_id = NULLIF(current_setting('mmp.org_id', true), '')::uuid);
```

Two lessons that shaped this and are worth not relearning:

1. **Test fixtures originally connected as the schema owner**, which bypasses
   RLS — so every cross-tenant assertion was vacuous. Fixtures now connect as
   `mmp_api`.
2. **`events` and `clicks` had no RLS** and `mmp_api` could read them. Found by
   audit, not by a test. Nothing queried them *yet*; a privilege that is only
   safe because nobody has used it is not a control. Enabling it was measured at
   no cost (13.2 ms → 12.0 ms per 5,000 rows).

## Authentication

- Passwords: **argon2id**.
- Sessions: cookies, `HttpOnly` + `Secure` + `SameSite`, with CSRF tokens on all
  unsafe methods.
- API keys: **HMAC-SHA256 under a KMS-held pepper**, so the database alone
  cannot verify a key.
- S2S: HMAC canonical-request signing with a nonce replay cache.
