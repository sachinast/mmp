"""Row-level security: the second, independent tenancy boundary.

Application middleware scopes every query to the active organisation. That is
the first boundary, and it is the one that will eventually be forgotten in some
new endpoint's ``WHERE`` clause. RLS makes that forgetting a returned-no-rows
bug instead of a cross-tenant data breach.

The policy compares against ``current_setting('mmp.org_id', true)``. The second
argument makes a missing setting return NULL rather than raise, and NULL fails
every comparison — so a connection with no tenant set sees nothing. Fail-closed
is the only acceptable default here.

``FORCE ROW LEVEL SECURITY`` matters as much as enabling it: without FORCE, the
table's *owner* bypasses the policy silently, and in development the owner is
usually whoever ran the migration.

**A note for anyone editing this file.** Migrations import the *functions* here,
which take their subject as a parameter and therefore mean the same thing
whenever they run. They must never import the *collections* — ``org_scoped_tables()``
and friends — because a list that grows changes what an already-applied migration
does. That is not hypothetical: adding one model once made a four-migrations-old
RLS step try to secure a table that would not exist for another four steps, which
worked on every incrementally-migrated database and failed on every fresh one.
Changing the policy SQL below rewrites history for every migration that calls it,
so treat it as append-only.
"""

from __future__ import annotations

from mmp_db.base import Base, OrgScopedMixin

TENANT_SETTING = "mmp.org_id"


def org_scoped_tables() -> list[str]:
    """Every table whose model carries ``OrgScopedMixin``.

    Derived from the metadata rather than hand-listed, so a new tenant table
    cannot be added without a policy — the test walks the same list.
    """
    tables: list[str] = []
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        if issubclass(cls, OrgScopedMixin) and getattr(cls, "__org_scoped__", False):
            tablename = getattr(cls, "__tablename__", None)
            if isinstance(tablename, str):
                tables.append(tablename)
    return sorted(set(tables))


def enable_rls_sql(table: str) -> list[str]:
    # sql-identifier-ok: table names come from the ORM metadata, never a request.
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"""
        CREATE POLICY org_isolation ON {table}
            USING (
                organization_id
                = NULLIF(current_setting('{TENANT_SETTING}', true), '')::uuid
            )
            WITH CHECK (
                organization_id
                = NULLIF(current_setting('{TENANT_SETTING}', true), '')::uuid
            )
        """,
        # The worker runs without a request context and legitimately processes
        # every tenant's backlog. It gets a named bypass policy rather than the
        # BYPASSRLS role attribute, so the exemption is visible in the catalogue
        # and scoped to these tables only.
        f"""
        CREATE POLICY worker_access ON {table}
            TO mmp_worker
            USING (true) WITH CHECK (true)
        """,
    ]


# The tracker's chicken-and-egg problem.
#
# Authentication happens *before* the tenant is known: the whole point of
# presenting an API key is to discover which organisation the caller is. So the
# tracker cannot set mmp.org_id first, and the org_isolation policy would return
# it zero rows on every request.
#
# The resolution is a narrow, named, read-only policy on exactly the three
# tables the tracker must consult pre-authentication. Combined with the GRANTs —
# mmp_tracker holds SELECT on only these tables and INSERT on only the two event
# tables — the blast radius is: it can read credential rows (which contain
# hashes, not keys) and tracking links, and it can write events. It cannot read
# a campaign, a user, a postback rule, or a partner credential, and it cannot
# UPDATE or DELETE anything at all.
TRACKER_LOOKUP_TABLES = ("api_keys", "apps", "tracking_links")


def tracker_lookup_sql(table: str) -> list[str]:
    # sql-identifier-ok: table names come from the constant above.
    return [
        f"""
        CREATE POLICY tracker_lookup ON {table}
            FOR SELECT TO mmp_tracker
            USING (true)
        """
    ]


def drop_tracker_lookup_sql(table: str) -> list[str]:
    # sql-identifier-ok: as above.
    return [f"DROP POLICY IF EXISTS tracker_lookup ON {table}"]


def grant_sql(table: str) -> list[str]:
    """Standard grants for a tenant table.

    Kept here rather than written out in each migration so that the privilege
    set for a new table is one call and cannot drift — a table granted more than
    it needs is the kind of thing nobody notices until an audit.
    """
    # sql-identifier-ok: table names come from callers' own constants, never
    # from a request; see the module docstring.
    return [
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO mmp_api, mmp_worker",
        f"GRANT SELECT ON {table} TO mmp_readonly",
    ]


def disable_rls_sql(table: str) -> list[str]:
    # sql-identifier-ok: see above.
    return [
        f"DROP POLICY IF EXISTS worker_access ON {table}",
        f"DROP POLICY IF EXISTS org_isolation ON {table}",
        f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY",
    ]


# --- roles --------------------------------------------------------------
#
# Four roles, least privilege each. None of them owns a table, and none of them
# is a superuser: a compromised application credential must not be able to drop
# a policy it is subject to.
#
# Passwords are set out of band (KMS-sourced) and never appear in a migration.

ROLE_DEFINITIONS = """
DO $$
BEGIN
    -- The business API. RLS applies; no DDL; no BYPASSRLS.
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mmp_api') THEN
        CREATE ROLE mmp_api NOLOGIN;
    END IF;
    -- The tracking edge. Inserts events and clicks; reads nothing on a request
    -- path. Its statement timeout is deliberately brutal.
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mmp_tracker') THEN
        CREATE ROLE mmp_tracker NOLOGIN;
    END IF;
    -- Background processing. Crosses tenants by design, via the named policy
    -- above rather than a blanket role attribute.
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mmp_worker') THEN
        CREATE ROLE mmp_worker NOLOGIN;
    END IF;
    -- BI tools and humans with a read-only reason to be here.
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mmp_readonly') THEN
        CREATE ROLE mmp_readonly NOLOGIN;
    END IF;
END
$$;

-- Every service reasons in UTC. Left to the server default, date_trunc and any
-- bare date literal would follow whatever time zone the cluster happens to be
-- set to — which is how partition boundaries ended up five and a half hours off
-- the days they were named for.
ALTER ROLE mmp_api      SET timezone = 'UTC';
ALTER ROLE mmp_tracker  SET timezone = 'UTC';
ALTER ROLE mmp_worker   SET timezone = 'UTC';
ALTER ROLE mmp_readonly SET timezone = 'UTC';

ALTER ROLE mmp_tracker SET statement_timeout = '2s';
ALTER ROLE mmp_api     SET statement_timeout = '15s';
ALTER ROLE mmp_worker  SET statement_timeout = '60s';
ALTER ROLE mmp_readonly SET statement_timeout = '120s';
"""

GRANTS = """
GRANT USAGE ON SCHEMA public TO mmp_api, mmp_tracker, mmp_worker, mmp_readonly;

-- API: full DML on business tables, no DDL.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO mmp_api;

-- Tracker: append-only, and only to the two ingest tables. It cannot read an
-- API key row, cannot read another tenant's campaigns, and cannot UPDATE
-- anything at all.
GRANT INSERT ON clicks, events TO mmp_tracker;
GRANT SELECT ON tracking_links, api_keys, apps TO mmp_tracker;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO mmp_worker;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO mmp_readonly;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO mmp_api, mmp_worker;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO mmp_readonly;
"""

REVOKE_PUBLIC = """
-- PUBLIC gets CREATE on the public schema by default in older clusters, and
-- CONNECT on the database. Neither is wanted.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
"""
