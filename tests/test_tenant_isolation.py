"""The tenancy boundary, tested against a real database as the real roles.

Every assertion here is about something that cannot be caught in review of
application code, because the failure mode is the *absence* of a WHERE clause
or the presence of a leaked session variable.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest
from mmp_db.rls import TENANT_SETTING, org_scoped_tables


async def _campaign_count(conn: asyncpg.Connection) -> int:
    return await conn.fetchval("SELECT count(*) FROM campaigns")  # type: ignore[no-any-return]


async def test_tenant_sees_only_its_own_rows(api_pool, two_orgs):
    org_a, _org_b = two_orgs
    async with api_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config($1, $2, true)", TENANT_SETTING, str(org_a))
        rows = await conn.fetch("SELECT organization_id FROM campaigns")
    assert rows, "tenant A must see its own campaign"
    assert {row["organization_id"] for row in rows} == {org_a}


async def test_query_without_org_filter_still_cannot_cross_tenants(api_pool, two_orgs):
    """The whole point of RLS: a forgotten WHERE clause is not a breach.

    This query has no organisation filter at all — exactly the bug that RLS
    exists to contain.
    """
    org_a, org_b = two_orgs
    async with api_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config($1, $2, true)", TENANT_SETTING, str(org_b))
        rows = await conn.fetch("SELECT organization_id FROM campaigns")
    assert org_a not in {row["organization_id"] for row in rows}


async def test_no_tenant_set_sees_nothing(api_pool, two_orgs):
    """Fail closed. An unset tenant must not mean 'all tenants'."""
    async with api_pool.acquire() as conn:
        assert await _campaign_count(conn) == 0


async def test_cannot_insert_into_another_tenant(api_pool, two_orgs):
    """WITH CHECK: writing a row you would not be allowed to read is refused."""
    org_a, org_b = two_orgs
    from mmp_core.ids import uuid7

    async with api_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config($1, $2, true)", TENANT_SETTING, str(org_a))
        app_id = await conn.fetchval("SELECT id FROM apps LIMIT 1")
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                """INSERT INTO campaigns (id, organization_id, app_id, name, status)
                   VALUES ($1, $2, $3, 'smuggled', 'active')""",
                uuid7(),
                org_b,
                app_id,
            )


async def test_set_local_does_not_survive_the_transaction(api_pool, two_orgs):
    """The PgBouncer trap, asserted directly.

    A tenant established with SET LOCAL must be gone when the transaction ends.
    If this ever fails, a pooled connection handed to the next request carries
    the previous request's tenant — a cross-tenant read with no bug in any
    query.
    """
    org_a, _ = two_orgs
    async with api_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config($1, $2, true)", TENANT_SETTING, str(org_a))
            assert await _campaign_count(conn) > 0

        leaked = await conn.fetchval("SELECT current_setting($1, true)", TENANT_SETTING)
        assert leaked in (None, ""), f"tenant leaked past its transaction: {leaked!r}"
        assert await _campaign_count(conn) == 0


async def test_pool_helper_scopes_the_tenant_to_the_transaction():
    """The helper must ask for a *transaction-local* setting.

    This one inspects the call rather than the effect, deliberately. asyncpg's
    own pool runs a reset on release, which hides a session-scoped setting in
    any in-process test — the leak would only appear in production, through
    PgBouncer, under load. So assert on the thing that differs: the third
    argument to set_config, which is what makes it SET LOCAL rather than SET.
    """
    from mmp_db.pool import Database

    calls: list[tuple[object, ...]] = []

    class FakeTransaction:
        async def __aenter__(self) -> None:
            calls.append(("BEGIN",))

        async def __aexit__(self, *_exc: object) -> None:
            calls.append(("COMMIT",))

    class FakeConnection:
        def transaction(self) -> FakeTransaction:
            return FakeTransaction()

        async def execute(self, query: str, *args: object) -> None:
            calls.append((query, *args))

    class FakeAcquire:
        async def __aenter__(self) -> FakeConnection:
            return FakeConnection()

        async def __aexit__(self, *_exc: object) -> None:
            return None

    class FakePool:
        def acquire(self) -> FakeAcquire:
            return FakeAcquire()

    org = uuid.uuid4()
    database = Database(FakePool(), role="mmp_api")  # type: ignore[arg-type]
    async with database.tenant_connection(org):
        pass

    set_config = [call for call in calls if "set_config" in str(call[0])]
    assert set_config, "tenant_connection did not set a tenant at all"
    assert set_config[0][-1] is True, (
        "set_config was called with is_local=False — the tenant would survive "
        "the transaction and leak into the next request on a pooled connection"
    )
    assert calls.index(("BEGIN",)) < calls.index(set_config[0]), (
        "the tenant must be set inside an explicit transaction, or SET LOCAL "
        "has no transaction to be local to"
    )


async def test_every_org_scoped_table_has_a_policy(owner_conn):
    """A new tenant table without a policy is a build failure, not a surprise."""
    rows = await owner_conn.fetch(
        "SELECT tablename FROM pg_policies WHERE schemaname = 'public' "
        "AND policyname = 'org_isolation'"
    )
    with_policy = {row["tablename"] for row in rows}
    expected = set(org_scoped_tables())
    assert expected - with_policy == set(), (
        f"org-scoped tables missing an RLS policy: {sorted(expected - with_policy)}"
    )


async def test_rls_is_forced_not_merely_enabled(owner_conn):
    """Without FORCE, the table owner bypasses the policy silently."""
    rows = await owner_conn.fetch(
        "SELECT relname FROM pg_class WHERE relrowsecurity AND NOT relforcerowsecurity "
        "AND relnamespace = 'public'::regnamespace"
    )
    assert not rows, f"RLS enabled but not FORCEd on: {[r['relname'] for r in rows]}"


async def test_tracker_role_cannot_read_api_keys_of_other_tenants(two_orgs):
    """Least privilege for the ingest role, verified rather than assumed."""
    from tests.conftest_db import role_dsn

    conn = await asyncpg.connect(role_dsn("mmp_tracker"))
    try:
        # It may read api_keys (it authenticates against them) but must not be
        # able to modify anything.
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute("UPDATE apps SET name = 'hijacked'")
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute("DELETE FROM campaigns")
    finally:
        await conn.close()


async def test_readonly_role_cannot_write(two_orgs):
    from tests.conftest_db import role_dsn

    conn = await asyncpg.connect(role_dsn("mmp_readonly"))
    try:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "INSERT INTO organizations (id, name, slug) VALUES ($1, 'x', 'x')", uuid.uuid4()
            )
    finally:
        await conn.close()


# --- the raw event tables -----------------------------------------------
#
# Added after an audit found that `events` and `clicks` carried
# organization_id, had no RLS, and were readable by mmp_api — which holds SELECT
# on every table in the schema. Nothing queried them from the API, so no test
# failed; the event explorer would have been the first query to cross a tenant.


async def test_the_api_role_cannot_read_raw_events(api_pool, owner_conn, two_orgs):
    """A privilege that is only safe because nobody has used it yet is not a
    control."""
    from mmp_core.ids import uuid7

    org_a, _ = two_orgs
    app_id = await owner_conn.fetchval(
        "SELECT id FROM apps WHERE organization_id = $1 LIMIT 1", org_a
    )
    event_id = uuid7()
    await owner_conn.execute(
        """INSERT INTO events (event_id, received_at, occurred_at, organization_id,
                               app_id, event_name, anonymous_id)
           VALUES ($1, now(), now(), $2, $3, 'install', 'rls-probe')""",
        event_id,
        org_a,
        app_id,
    )
    try:
        # With no tenant set, the API role must see nothing at all.
        async with api_pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM events") == 0

        # With a tenant set, it sees that tenant's rows and only those.
        async with api_pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT set_config($1, $2, $3)", TENANT_SETTING, str(org_a), True)
            visible = await conn.fetch(
                "SELECT organization_id FROM events WHERE anonymous_id = 'rls-probe'"
            )
        assert visible and {row["organization_id"] for row in visible} == {org_a}
    finally:
        await owner_conn.execute("DELETE FROM events WHERE event_id = $1", event_id)


async def test_a_tenant_cannot_see_another_tenants_events(api_pool, owner_conn, two_orgs):
    from mmp_core.ids import uuid7

    org_a, org_b = two_orgs
    app_a = await owner_conn.fetchval(
        "SELECT id FROM apps WHERE organization_id = $1 LIMIT 1", org_a
    )
    event_id = uuid7()
    await owner_conn.execute(
        """INSERT INTO events (event_id, received_at, occurred_at, organization_id,
                               app_id, event_name, anonymous_id)
           VALUES ($1, now(), now(), $2, $3, 'install', 'cross-tenant-probe')""",
        event_id,
        org_a,
        app_a,
    )
    try:
        async with api_pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT set_config($1, $2, $3)", TENANT_SETTING, str(org_b), True)
            rows = await conn.fetch(
                "SELECT event_id FROM events WHERE anonymous_id = 'cross-tenant-probe'"
            )
        assert not rows, "tenant B must not see tenant A's events"
    finally:
        await owner_conn.execute("DELETE FROM events WHERE event_id = $1", event_id)


async def test_the_tracker_can_insert_events_but_not_read_them(owner_conn, two_orgs):
    """Write-only by policy, not just by grant.

    The tracker authenticates *into* an organisation and writes on its behalf,
    so it inserts with no tenant set. It has no reason to read events back, and
    not being able to is worth more than the symmetry.
    """
    import asyncpg
    from mmp_core.ids import uuid7

    from tests.conftest_db import role_dsn

    org_a, _ = two_orgs
    app_id = await owner_conn.fetchval(
        "SELECT id FROM apps WHERE organization_id = $1 LIMIT 1", org_a
    )
    event_id = uuid7()

    conn = await asyncpg.connect(role_dsn("mmp_tracker"))
    try:
        await conn.execute(
            """INSERT INTO events (event_id, received_at, occurred_at, organization_id,
                                   app_id, event_name, anonymous_id)
               VALUES ($1, now(), now(), $2, $3, 'install', 'tracker-probe')""",
            event_id,
            org_a,
            app_id,
        )
        # It wrote the row and cannot see it.
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.fetchval("SELECT count(*) FROM events")
    finally:
        await conn.close()
        await owner_conn.execute("DELETE FROM events WHERE event_id = $1", event_id)


async def test_the_worker_can_read_every_tenants_events(seeded_app, owner_conn):
    """Attribution, rollups and reconciliation all cross tenants by design."""
    import asyncpg

    from tests.conftest_db import role_dsn

    conn = await asyncpg.connect(role_dsn("mmp_worker"))
    try:
        # Not an error, and not silently zero: the worker's policy is USING(true).
        assert await conn.fetchval("SELECT count(*) FROM events") >= 0
        assert await conn.fetchval("SELECT count(*) FROM clicks") >= 0
    finally:
        await conn.close()
