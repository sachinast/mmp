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
from mmp_db.sql import drop_table

CREATE_ALL = (
    """CREATE TABLE IF NOT EXISTS rollup_events_hourly (
    organization_id uuid        NOT NULL,
    app_id          uuid        NOT NULL,
    bucket_hour     timestamptz NOT NULL,
    event_name      text        NOT NULL,
    platform        smallint    NOT NULL DEFAULT 0,
    event_count     bigint      NOT NULL DEFAULT 0,
    -- Distinct devices *within this hour*. Deliberately not summable or
    -- maxable into a period total: see the note on distinct counts below.
    unique_devices  bigint      NOT NULL DEFAULT 0,
    revenue_minor   bigint      NOT NULL DEFAULT 0,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (app_id, bucket_hour, event_name, platform)
)
    """,
    """CREATE TABLE IF NOT EXISTS rollup_clicks_hourly (
    organization_id uuid        NOT NULL,
    app_id          uuid        NOT NULL,
    bucket_hour     timestamptz NOT NULL,
    -- NOT NULL with a nil-UUID sentinel rather than a nullable column.
    -- A primary key cannot contain NULL, and more importantly NULLs do not
    -- compare equal: with a nullable campaign_id the ON CONFLICT clause below
    -- would never match, so every refresh would insert a duplicate row for
    -- unattached clicks instead of updating the existing one.
    campaign_id     uuid        NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000',
    platform        smallint    NOT NULL DEFAULT 0,
    click_count     bigint      NOT NULL DEFAULT 0,
    bot_count       bigint      NOT NULL DEFAULT 0,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (app_id, bucket_hour, campaign_id, platform)
)
    """,
    """CREATE TABLE IF NOT EXISTS rollup_campaign_daily (
    organization_id uuid        NOT NULL,
    app_id          uuid        NOT NULL,
    bucket_day      date        NOT NULL,
    campaign_id     uuid        NOT NULL,
    clicks          bigint      NOT NULL DEFAULT 0,
    installs        bigint      NOT NULL DEFAULT 0,
    organic_installs bigint     NOT NULL DEFAULT 0,
    revenue_minor   bigint      NOT NULL DEFAULT 0,
    conversions     bigint      NOT NULL DEFAULT 0,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (app_id, bucket_day, campaign_id)
)
    """,
    """CREATE INDEX IF NOT EXISTS ix_rollup_events_hourly_org
       ON rollup_events_hourly (organization_id, bucket_hour)
    """,
    """CREATE INDEX IF NOT EXISTS ix_rollup_clicks_hourly_org
       ON rollup_clicks_hourly (organization_id, bucket_hour)
    """,
    """CREATE INDEX IF NOT EXISTS ix_rollup_campaign_daily_org
       ON rollup_campaign_daily (organization_id, bucket_day)
    """,
)

# Written out rather than imported from mmp_db.rollups.TABLES: a migration must
# not ask the present what the past meant. If that constant grows, this migration
# must keep doing what it did on the day it was written.
TABLES = ("rollup_events_hourly", "rollup_clicks_hourly", "rollup_campaign_daily")

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
