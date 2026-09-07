"""Rebuild event partitions with explicit UTC boundaries.

A bare date literal in a partition bound on a timestamptz column is resolved
using the session time zone of whoever ran the DDL. Created from a machine set
to Asia/Kolkata, every partition covered 18:30 UTC to 18:30 UTC — five and a
half hours off the day in its own name.

Two consequences, both silent: retention would have dropped the wrong slice of
data, and partitions created by a developer would not have lined up with
partitions created by CI.

This drops and recreates the partitions with '+00' bounds. It **refuses to run
if any partition holds rows**, because silently moving customer events between
partitions is not something a migration should do unsupervised. If this fails on
a database with data, the fix is a deliberate backfill: create correctly-bounded
partitions alongside, copy, verify counts, then swap.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from alembic import op
from mmp_db.partitions import PARTITION_INDEXES, PartitionSpec, create_partition_sql

from mmp_db import sql

revision: str = "802dff05d387"
down_revision: str | None = "b2f02dfa5bbb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REBUILD_DAYS_BACK = 2
REBUILD_DAYS_FORWARD = 8


# Pinning the session time zone for every application role is the second half
# of the fix. The bounds above are now explicit, but date_trunc in the rollup
# jobs and any future bare date literal would still follow the cluster's time
# zone. Both halves are needed: explicit bounds so the DDL is unambiguous, and a
# pinned session so nothing else drifts.
PIN_ROLE_TIMEZONES = """
ALTER ROLE mmp_api      SET timezone = 'UTC';
ALTER ROLE mmp_tracker  SET timezone = 'UTC';
ALTER ROLE mmp_worker   SET timezone = 'UTC';
ALTER ROLE mmp_readonly SET timezone = 'UTC';
"""


def upgrade() -> None:
    op.execute(PIN_ROLE_TIMEZONES)
    connection = op.get_bind()

    existing = connection.exec_driver_sql(
        """
        SELECT c.relname, p.relname AS parent
        FROM pg_class c
        JOIN pg_inherits i ON i.inhrelid = c.oid
        JOIN pg_class p ON p.oid = i.inhparent
        WHERE p.relname IN ('events', 'clicks')
        ORDER BY c.relname
        """
    ).fetchall()

    populated = []
    for name, _parent in existing:
        count = connection.exec_driver_sql(sql.count_rows(name)).scalar()
        if count:
            populated.append(f"{name} ({count} rows)")

    if populated:
        raise RuntimeError(
            "refusing to rebuild partitions that contain data: "
            + ", ".join(populated)
            + ". Rebuilding would move rows between partitions unsupervised; "
            "perform a deliberate backfill instead."
        )

    for name, _parent in existing:
        op.execute(sql.drop_table(name))

    today = dt.datetime.now(dt.UTC).date()
    start = today - dt.timedelta(days=REBUILD_DAYS_BACK)
    for table in PARTITION_INDEXES:
        for offset in range(REBUILD_DAYS_BACK + REBUILD_DAYS_FORWARD):
            spec = PartitionSpec(table, start + dt.timedelta(days=offset))
            for statement in create_partition_sql(spec):
                op.execute(statement)


def downgrade() -> None:
    # The previous bounds depended on the session time zone of whoever ran the
    # original migration, so there is no well-defined state to return to.
    pass
