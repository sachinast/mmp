"""Database access, with tenancy that cannot leak between requests.

Read this before opening a connection anywhere else in the codebase.

Under PgBouncer in transaction pooling mode — which is how this runs in
production — a backend connection is returned to the pool the instant a
transaction commits, and handed to whoever asks next. A tenant set with plain
``SET`` survives that handoff and silently applies to the next request, on
behalf of a different organisation. That is a cross-tenant read with no bug
anywhere in the query code, and it is close to undetectable in review.

So: ``tenant_connection`` is the only supported way to obtain a connection for
request-scoped work. It always opens an explicit transaction and always uses
``SET LOCAL``, whose lifetime is the transaction. There is a test asserting a
connection returned to the pool carries no tenant setting.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from mmp_core.logging import get_logger
from mmp_core.settings import Settings

from mmp_db.rls import TENANT_SETTING

log = get_logger(__name__)


class Database:
    """An asyncpg pool bound to one application role."""

    def __init__(self, pool: asyncpg.Pool[Any], *, role: str) -> None:
        self._pool = pool
        self.role = role

    @classmethod
    async def connect(
        cls,
        settings: Settings,
        *,
        role: str = "mmp_api",
        min_size: int | None = None,
        max_size: int | None = None,
    ) -> Database:
        pool = await asyncpg.create_pool(
            settings.asyncpg_dsn(),
            min_size=min_size or settings.db_pool_min,
            max_size=max_size or settings.db_pool_max,
            # PgBouncer in transaction mode cannot support server-side prepared
            # statements across connections. Leaving the cache on produces
            # intermittent "prepared statement does not exist" errors that only
            # appear under load, which is the worst possible time to find out.
            statement_cache_size=0,
            command_timeout=settings.db_statement_timeout_ms / 1000,
            server_settings={"application_name": f"mmp-{settings.service_name}"},
        )
        if pool is None:  # pragma: no cover — asyncpg only returns None on misuse
            raise RuntimeError("failed to create connection pool")
        log.info("db_pool_ready", role=role, min_size=pool.get_min_size())
        return cls(pool, role=role)

    async def close(self) -> None:
        await self._pool.close()

    async def ping(self) -> None:
        """Readiness probe."""
        async with self._pool.acquire() as conn:
            await conn.execute("SELECT 1")

    @asynccontextmanager
    async def tenant_connection(
        self, organization_id: uuid.UUID
    ) -> AsyncIterator[asyncpg.Connection[Any]]:
        """A connection scoped to one organisation for one transaction.

        Every request-path query goes through here. The RLS policies read the
        setting this establishes, so a query that forgets its own
        ``WHERE organization_id = ...`` returns that tenant's rows only.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # SET LOCAL, not SET: scoped to this transaction, gone on commit.
            # The third argument is is_local: true scopes the setting to this
            # transaction. Passed as a bind parameter rather than inlined so
            # that it is visible as data — and assertable in a test.
            await conn.execute(
                "SELECT set_config($1, $2, $3)", TENANT_SETTING, str(organization_id), True
            )
            yield conn

    @asynccontextmanager
    async def system_connection(self) -> AsyncIterator[asyncpg.Connection[Any]]:
        """A connection with no tenant set.

        For work that legitimately spans organisations — the rollup worker, the
        partition maintenance job, reconciliation. Under RLS this sees nothing
        on org-scoped tables unless the role holds the named worker policy,
        which is what keeps this from being an accidental back door.
        """
        async with self._pool.acquire() as conn:
            yield conn

    @asynccontextmanager
    async def acquire_raw(self) -> AsyncIterator[asyncpg.Connection[Any]]:
        """Unscoped access for ingest writes into non-RLS partitioned tables."""
        async with self._pool.acquire() as conn:
            yield conn
