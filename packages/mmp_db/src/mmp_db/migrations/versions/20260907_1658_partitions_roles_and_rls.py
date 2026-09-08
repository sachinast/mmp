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
from mmp_db.partitions import PartitionSpec, create_partition_sql
from mmp_db.rls import (
    GRANTS,
    REVOKE_PUBLIC,
    ROLE_DEFINITIONS,
    disable_rls_sql,
    enable_rls_sql,
)

revision: str = "514babc412b0"
down_revision: str | None = "be753243702d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enough partitions to cover the migration itself plus the first week of
# operation. After that the maintenance job keeps a rolling window ahead.
# The org-scoped tables **as of this migration**, written out rather than derived
# from the ORM metadata.
#
# The first version called org_scoped_tables(), which reads the live models. That
# makes an applied migration change behaviour as the codebase evolves: adding a
# WebhookDelivery model four migrations later made *this* one try to enable RLS
# on a table that would not exist for another four steps. It worked on every
# database that had migrated incrementally and failed on every fresh one — so it
# passed locally and would have failed in CI.
#
# A migration is a snapshot of an intent at a point in time. It must not ask the
# present what the past meant.
# The table definitions as they were when this migration was written. Inlined
# for the same reason as the table list below: a migration must not ask the
# present what the past meant, and a DDL constant is free to gain a column.
PARENTS = (
    """CREATE TABLE IF NOT EXISTS clicks (
    click_id          uuid        NOT NULL,
    clicked_at        timestamptz NOT NULL,
    organization_id   uuid        NOT NULL,
    app_id            uuid        NOT NULL,
    campaign_id       uuid,
    tracking_link_id  uuid        NOT NULL,
    device_hash       bytea,
    ip_hash           bytea,
    country           char(2),
    platform          smallint,
    os_version        text,
    device_model      text,
    user_agent        text,
    sub1              text,
    sub2              text,
    sub3              text,
    is_bot            boolean     NOT NULL DEFAULT false,
    PRIMARY KEY (clicked_at, click_id)
) PARTITION BY RANGE (clicked_at);
    """,
    """CREATE TABLE IF NOT EXISTS events (
    event_id        uuid        NOT NULL,
    received_at     timestamptz NOT NULL,
    occurred_at     timestamptz NOT NULL,
    organization_id uuid        NOT NULL,
    app_id          uuid        NOT NULL,
    event_name      text        NOT NULL,
    anonymous_id    text        NOT NULL,
    user_id         text,
    session_id      uuid,
    platform        smallint,
    os_version      text,
    app_version     text,
    device_model    text,
    country         char(2),
    ip_hash         bytea,
    click_id        uuid,
    revenue_minor   bigint,
    currency        char(3),
    clock_skew_ms   bigint,
    properties      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (received_at, app_id, event_id)
) PARTITION BY RANGE (received_at);
    """,
)

ORG_SCOPED_TABLES = (
    "api_keys",
    "apps",
    "attributions",
    "audit_log",
    "campaigns",
    "consent_states",
    "conversion_mappings",
    "deep_links",
    "postback_deliveries",
    "postback_rules",
    "provider_integrations",
    "tracking_links",
    "usage_rollup",
    "webhooks",
)

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

    for table in ORG_SCOPED_TABLES:
        for statement in enable_rls_sql(table):
            op.execute(statement)


def downgrade() -> None:
    for table in ORG_SCOPED_TABLES:
        for statement in disable_rls_sql(table):
            op.execute(statement)
    op.execute("DROP TABLE IF EXISTS events CASCADE")
    op.execute("DROP TABLE IF EXISTS clicks CASCADE")
    # Roles are intentionally not dropped: they may own grants in other
    # databases in the same cluster, and a migration should not reach outside
    # its own database to clean up.
