"""Pre-aggregated reporting tables.

On Postgres these are the read path, not an optimisation: a dashboard querying
raw events is fine at ten million rows and unusable at a billion, and the
transition happens without warning on whichever advertiser grows fastest.

Not ORM-mapped. They are written by one worker with hand-written aggregate SQL
and read by the analytics API with hand-written queries; routing either through
SQLAlchemy would cost more than it explains.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from mmp_db.rls import enable_rls_sql, grant_sql
from mmp_db.rollups import CREATE_ALL, TABLES
from mmp_db.sql import drop_table

revision: str = "26e63884a540"
down_revision: str | None = "802dff05d387"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for statement in CREATE_ALL:
        op.execute(statement)

    # The rollups are tenant data and must be subject to the same isolation as
    # everything else — an analytics endpoint reading them with a forgotten
    # filter would otherwise expose one advertiser's numbers to another.
    #
    # Reusing the shared helpers rather than writing the policies out here: the
    # isolation rule should have one definition, and a migration that spells it
    # out again is a migration that can spell it out differently.
    for table in TABLES:
        for statement in enable_rls_sql(table):
            op.execute(statement)
        for statement in grant_sql(table):
            op.execute(statement)


def downgrade() -> None:
    for table in TABLES:
        op.execute(drop_table(table))
