"""Partition maintenance.

Runs on a schedule from the worker. Two jobs, both of which must be boring:

* **Create ahead.** Partitions are created a week in advance. If the job stops,
  there is a week of warning before inserts start failing with "no partition of
  relation found for row" — which is a page, not an outage, provided the lead
  time is real. One day of lead time is not real.
* **Drop behind.** Retention is enforced by dropping whole partitions. Nothing
  is deleted row by row.

Dropping is guarded: the job refuses to drop a partition newer than the
retention floor even if asked, and logs every drop with its row count first, so
a misconfigured retention value leaves evidence rather than a silence.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import asyncpg
from mmp_core.logging import get_logger

from mmp_db.partitions import (
    PARTITION_INDEXES,
    PartitionSpec,
    create_partition_sql,
    drop_partition_sql,
)

log = get_logger(__name__)

DEFAULT_LEAD_DAYS = 7
# Hot retention. Cold storage export (Phase 12) runs before anything is dropped.
DEFAULT_RETENTION_DAYS = 90
# Nothing younger than this is ever dropped, whatever the caller passes.
MINIMUM_RETENTION_DAYS = 30


async def ensure_partitions(
    conn: asyncpg.Connection[Any],
    *,
    today: dt.date | None = None,
    lead_days: int = DEFAULT_LEAD_DAYS,
) -> list[str]:
    """Create any missing partition for today through ``lead_days`` ahead."""
    today = today or dt.datetime.now(dt.UTC).date()
    created: list[str] = []
    for table in PARTITION_INDEXES:
        for offset in range(lead_days + 1):
            spec = PartitionSpec(table, today + dt.timedelta(days=offset))
            for statement in create_partition_sql(spec):
                await conn.execute(statement)
            created.append(spec.name)
    log.info("partitions_ensured", count=len(created), lead_days=lead_days)
    return created


async def existing_partitions(conn: asyncpg.Connection[Any], table: str) -> list[str]:
    rows = await conn.fetch(
        """
        SELECT c.relname
        FROM pg_class c
        JOIN pg_inherits i ON i.inhrelid = c.oid
        JOIN pg_class p ON p.oid = i.inhparent
        WHERE p.relname = $1
        ORDER BY c.relname
        """,
        table,
    )
    return [row["relname"] for row in rows]


async def drop_expired_partitions(
    conn: asyncpg.Connection[Any],
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    today: dt.date | None = None,
    dry_run: bool = True,
) -> list[str]:
    """Drop partitions entirely older than the retention window.

    Defaults to ``dry_run=True``. Data deletion is not something a scheduled job
    should do because a default made it easy — the caller states the intent.
    """
    if retention_days < MINIMUM_RETENTION_DAYS:
        raise ValueError(
            f"retention_days={retention_days} is below the {MINIMUM_RETENTION_DAYS}-day floor"
        )

    today = today or dt.datetime.now(dt.UTC).date()
    cutoff = today - dt.timedelta(days=retention_days)
    dropped: list[str] = []

    for table in PARTITION_INDEXES:
        for name in await existing_partitions(conn, table):
            try:
                day = dt.datetime.strptime(name.removeprefix(f"{table}_"), "%Y%m%d").date()
            except ValueError:  # pragma: no cover — a partition we did not create
                log.warning("partition_name_unrecognised", partition=name)
                continue
            if day >= cutoff:
                continue
            spec = PartitionSpec(table, day)
            if dry_run:
                log.info("partition_drop_planned", partition=spec.name, cutoff=str(cutoff))
            else:
                log.info("partition_dropping", partition=spec.name, cutoff=str(cutoff))
                await conn.execute(drop_partition_sql(spec))
            dropped.append(spec.name)

    return dropped
