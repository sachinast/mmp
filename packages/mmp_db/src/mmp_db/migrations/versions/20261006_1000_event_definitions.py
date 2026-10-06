"""Event definitions: a per-app catalogue of names, and the ability to block one.

Until now an event was whatever string the SDK sent. That is still true for
ingest — nothing here gates it — but an advertiser integrating an app needs a
list to work from, a place to say what ``withdrawal_requested`` means, and a way
to stop a misfiring event polluting reports without shipping an app update.

``status = 'blocked'`` is the one policy a definition carries. The tracker reads
the blocked names for an app (through Redis, with this table as the fallback)
and drops those events before they are queued — the same place consent is
applied, for the same reason: dropped after persistence is a deletion problem.

The tracker is granted SELECT on three columns only. It needs the names that are
blocked for an app and nothing else about a definition.

Revision ID: a1c4e7b90d23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1c4e7b90d23"
down_revision: str | None = "f3c9a1e27d48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_definitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("app_id", postgresql.UUID(as_uuid=True), nullable=False),
        # The exact name as the SDK sends it. Names are stored as sent on the
        # events table, so the definition must match that form, not a folded one.
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(length=40), nullable=False, server_default="custom"),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="custom"),
        sa.Column("revenue", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('standard', 'custom')", name="kind_valid"),
        sa.CheckConstraint("status IN ('active', 'blocked')", name="status_valid"),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_event_definitions_organization_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["app_id"],
            ["apps.id"],
            name=op.f("fk_event_definitions_app_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_definitions")),
        sa.UniqueConstraint("app_id", "name", name="uq_event_definitions_app_name"),
    )
    op.create_index(
        op.f("ix_event_definitions_organization_id"), "event_definitions", ["organization_id"]
    )
    op.create_index(op.f("ix_event_definitions_app_id"), "event_definitions", ["app_id"])

    op.execute("ALTER TABLE event_definitions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE event_definitions FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY org_isolation ON event_definitions
            USING (
                organization_id = NULLIF(current_setting('mmp.org_id', true), '')::uuid
            )
            WITH CHECK (
                organization_id = NULLIF(current_setting('mmp.org_id', true), '')::uuid
            )
        """
    )
    op.execute(
        """
        CREATE POLICY worker_access ON event_definitions
            TO mmp_worker USING (true) WITH CHECK (true)
        """
    )
    # The tracker resolves blocked names before it knows the tenant — the same
    # chicken-and-egg as API keys — so it gets a narrow lookup policy and the
    # three columns that question needs.
    op.execute(
        """
        CREATE POLICY tracker_lookup ON event_definitions
            FOR SELECT TO mmp_tracker
            USING (true)
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON event_definitions TO mmp_api, mmp_worker")
    op.execute("GRANT SELECT (app_id, name, status) ON event_definitions TO mmp_tracker")
    op.execute("GRANT SELECT ON event_definitions TO mmp_readonly")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS event_definitions")
