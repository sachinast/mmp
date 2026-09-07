"""Database fixtures.

Tests connect **as the application roles**, not as the schema owner. Testing
isolation as the owner proves nothing: the owner is exempt from RLS unless
FORCE is set, and even with FORCE the grant surface is different. If these
fixtures connected as `postgres`, every isolation test below would pass while
production stayed wide open.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

TEST_DB = os.environ.get("MMP_TEST_DB", "mmp_test")
TEST_HOST = os.environ.get("MMP_TEST_HOST", "127.0.0.1")
TEST_PORT = int(os.environ.get("MMP_TEST_PORT", "5432"))

ROLE_PASSWORDS = {
    "mmp_api": "dev_only_api",
    "mmp_tracker": "dev_only_tracker",
    "mmp_worker": "dev_only_worker",
    "mmp_readonly": "dev_only_readonly",
}


def role_dsn(role: str) -> str:
    return f"postgresql://{role}:{ROLE_PASSWORDS[role]}@{TEST_HOST}:{TEST_PORT}/{TEST_DB}"


def owner_dsn() -> str:
    user = os.environ.get("MMP_TEST_OWNER", os.environ.get("USER", "postgres"))
    return f"postgresql://{user}@{TEST_HOST}:{TEST_PORT}/{TEST_DB}"


async def _database_available() -> bool:
    try:
        conn = await asyncpg.connect(owner_dsn(), timeout=2)
    except Exception:
        return False
    await conn.close()
    return True


@pytest_asyncio.fixture(scope="session")
async def db_available() -> bool:
    return await _database_available()


@pytest_asyncio.fixture
async def owner_conn(db_available: bool) -> AsyncIterator[asyncpg.Connection[Any]]:
    if not db_available:
        pytest.skip("mmp_test database not reachable — run `make db-create migrate dev-roles`")
    conn = await asyncpg.connect(owner_dsn())
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def api_pool(db_available: bool) -> AsyncIterator[asyncpg.Pool[Any]]:
    if not db_available:
        pytest.skip("mmp_test database not reachable")
    pool = await asyncpg.create_pool(
        role_dsn("mmp_api"), min_size=1, max_size=2, statement_cache_size=0
    )
    assert pool is not None
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def two_orgs(
    owner_conn: asyncpg.Connection[Any],
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """Two organisations with one campaign each — the isolation test fixture."""
    from mmp_core.ids import uuid7

    org_a, org_b = uuid7(), uuid7()
    app_a, app_b = uuid7(), uuid7()
    suffix = uuid.uuid4().hex[:8]

    for org, app, label in ((org_a, app_a, "a"), (org_b, app_b, "b")):
        await owner_conn.execute(
            "INSERT INTO organizations (id, name, slug, timezone) VALUES ($1, $2, $3, 'UTC')",
            org,
            f"org-{label}-{suffix}",
            f"org-{label}-{suffix}",
        )
        await owner_conn.execute(
            """INSERT INTO apps (id, organization_id, name, platform, status,
                                 install_window_days, event_window_days,
                                 session_timeout_minutes, timezone)
               VALUES ($1, $2, $3, 'android', 'active', 7, 30, 30, 'UTC')""",
            app,
            org,
            f"app-{label}",
        )
        await owner_conn.execute(
            """INSERT INTO campaigns (id, organization_id, app_id, name, source, status)
               VALUES ($1, $2, $3, $4, 'test', 'active')""",
            uuid7(),
            org,
            app,
            f"campaign-{label}-{suffix}",
        )
    try:
        yield org_a, org_b
    finally:
        for org in (org_a, org_b):
            await owner_conn.execute("DELETE FROM organizations WHERE id = $1", org)
