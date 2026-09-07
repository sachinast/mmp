"""Partitioned event storage, database roles, and row-level security.

This is the migration that makes tenant isolation a property of the database
rather than a property of our discipline. Three things land together because
they are one decision:

* the append-only event tables, which are partitioned and *not* org-scoped
  (they carry organization_id but are never queried on a request path);
* the four least-privilege roles, none of which owns a table;
* RLS policies on every org-scoped table, forced so that even the owner is
  subject to them.

Revision ID: 514babc412b0'
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from alembic import op
from mmp_db.partitions import PARENTS, PartitionSpec, create_partition_sql
from mmp_db.rls import (
    GRANTS,
    REVOKE_PUBLIC,
    ROLE_DEFINITIONS,
    disable_rls_sql,
    enable_rls_sql,
    org_scoped_tables,
)

revision: str = "514babc412b0"
down_revision: str | None = "be753243702d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enough partitions to cover the migration itself plus the first week of
# operation. After that the maintenance job keeps a rolling window ahead.
BOOTSTRAP_DAYS_BACK = 2
BOOTSTRAP_DAYS_FORWARD = 8


def upgrade() -> None:
    for statement in PARENTS:
        op.execute(statement)

    today = dt.datetime.now(dt.UTC).date()
    start = today - dt.timedelta(days=BOOTSTRAP_DAYS_BACK)
    for table in ("clicks", "events"):
        for offset in range(BOOTSTRAP_DAYS_BACK + BOOTSTRAP_DAYS_FORWARD):
            spec = PartitionSpec(table, start + dt.timedelta(days=offset))
            for statement in create_partition_sql(spec):
                op.execute(statement)

    op.execute(ROLE_DEFINITIONS)
    op.execute(REVOKE_PUBLIC)
    op.execute(GRANTS)

    for table in org_scoped_tables():
        for statement in enable_rls_sql(table):
            op.execute(statement)


def downgrade() -> None:
    for table in org_scoped_tables():
        for statement in disable_rls_sql(table):
            op.execute(statement)
    op.execute("DROP TABLE IF EXISTS events CASCADE")
    op.execute("DROP TABLE IF EXISTS clicks CASCADE")
    # Roles are intentionally not dropped: they may own grants in other
    # databases in the same cluster, and a migration should not reach outside
    # its own database to clean up.
