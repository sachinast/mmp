"""Let the tracker resolve an API key before it knows the tenant.

Authentication is the act of discovering which organisation the caller is, so
the tracker cannot set mmp.org_id before performing it — and the org_isolation
policy would therefore return it zero rows on every ingest request.

This adds a read-only policy for the mmp_tracker role on exactly the three
tables it must consult pre-authentication. Its GRANTs already limit it to SELECT
on those tables and INSERT on the two event tables, so it still cannot read a
campaign, a postback rule or a partner credential, and cannot write anywhere
outside the event stream.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from mmp_db.rls import TRACKER_LOOKUP_TABLES, drop_tracker_lookup_sql, tracker_lookup_sql

revision: str = "b2f02dfa5bbb"
down_revision: str | None = "8afb3aec00cf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in TRACKER_LOOKUP_TABLES:
        for statement in tracker_lookup_sql(table):
            op.execute(statement)


def downgrade() -> None:
    for table in TRACKER_LOOKUP_TABLES:
        for statement in drop_tracker_lookup_sql(table):
            op.execute(statement)
