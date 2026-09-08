"""SKAdNetwork install-validation postbacks.

Apple posts these to a public URL with no credential attached. The signature is
the entire authentication, so the table below only ever receives rows that
``mmp_attrib.skadnetwork.verify`` has already accepted — but the schema assumes
nothing about that and enforces what it can itself.

``transaction_id`` is globally unique, which is the replay control. Apple
documents that a postback is retried up to nine times over nine days if the
receiver does not answer 200, so duplicates are expected in normal operation,
not merely under attack. The constraint turns both cases into the same no-op.
It is deliberately not scoped per app: Apple's transaction ids are UUIDs and
uniqueness across the whole table means a postback cannot be replayed against a
*different* app to manufacture an install there.

``apps.apple_app_id`` is new because Apple identifies the app by its numeric App
Store id, which nothing here recorded — the schema had the bundle id, which the
postback does not carry.

The raw payload is kept whole. These are signed documents that an ad network may
dispute months later, and being able to re-verify the exact bytes we received is
the only way to settle it; a reconstruction from parsed columns proves nothing.

Not partitioned, unlike events and clicks. One postback per install per network
is orders of magnitude below event volume, and daily partitions would be
machinery maintained for a table that does not need it.

Revision ID: a7d2e91c4f08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7d2e91c4f08"
down_revision: str | None = "f2a7c3e14b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("apps", sa.Column("apple_app_id", sa.BigInteger(), nullable=True))
    # The lookup the postback endpoint performs on every request. Partial,
    # because most apps are Android-only and will never have one.
    op.create_index(
        "ix_apps_apple_app_id",
        "apps",
        ["apple_app_id"],
        unique=True,
        postgresql_where=sa.text("apple_app_id IS NOT NULL"),
    )

    op.create_table(
        "skadnetwork_postbacks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("app_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("version", sa.String(length=8), nullable=False),
        sa.Column("ad_network_id", sa.String(length=255), nullable=False),
        sa.Column("apple_app_id", sa.BigInteger(), nullable=False),
        sa.Column("transaction_id", sa.String(length=64), nullable=False),
        # campaign-id before version 4, source-identifier after. Stored as text
        # because version 4 widened it from a small integer to a value whose
        # precision Apple varies with the postback's data tier.
        sa.Column("source_identifier", sa.String(length=32), nullable=True),
        sa.Column("did_win", sa.Boolean(), nullable=False),
        sa.Column("redownload", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("fidelity_type", sa.SmallInteger(), nullable=True),
        # Not covered by Apple's signature in any version — see the note in
        # mmp_attrib.skadnetwork. Stored as reported, never as proven.
        sa.Column("conversion_value", sa.SmallInteger(), nullable=True),
        sa.Column("coarse_value", sa.String(length=10), nullable=True),
        sa.Column("postback_sequence_index", sa.SmallInteger(), nullable=True),
        sa.Column("source_app_id", sa.String(length=32), nullable=True),
        sa.Column("source_domain", sa.String(length=255), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_skadnetwork_postbacks_organization_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["app_id"],
            ["apps.id"],
            name=op.f("fk_skadnetwork_postbacks_app_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skadnetwork_postbacks")),
        # The replay control. See the module docstring for why it is global.
        sa.UniqueConstraint("transaction_id", name="uq_skadnetwork_postbacks_transaction"),
        sa.CheckConstraint(
            "conversion_value IS NULL OR conversion_value BETWEEN 0 AND 63",
            name="conversion_value_range",
        ),
        sa.CheckConstraint(
            "coarse_value IS NULL OR coarse_value IN ('low', 'medium', 'high')",
            name="coarse_value_valid",
        ),
    )
    op.create_index(
        "ix_skadnetwork_postbacks_app_received",
        "skadnetwork_postbacks",
        ["app_id", "received_at"],
    )
    op.create_index(
        op.f("ix_skadnetwork_postbacks_organization_id"),
        "skadnetwork_postbacks",
        ["organization_id"],
    )

    op.execute("ALTER TABLE skadnetwork_postbacks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE skadnetwork_postbacks FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY org_isolation ON skadnetwork_postbacks
            USING (organization_id = NULLIF(current_setting('mmp.org_id', true), '')::uuid)
            WITH CHECK (organization_id = NULLIF(current_setting('mmp.org_id', true), '')::uuid)
        """
    )
    # Write-only for the tracker, and no USING clause: it receives postbacks and
    # has no reason to read any back. The same shape as its access to events.
    op.execute(
        """
        CREATE POLICY tracker_insert ON skadnetwork_postbacks
            FOR INSERT TO mmp_tracker WITH CHECK (true)
        """
    )
    op.execute(
        """
        CREATE POLICY worker_access ON skadnetwork_postbacks
            TO mmp_worker USING (true) WITH CHECK (true)
        """
    )
    op.execute("GRANT SELECT ON skadnetwork_postbacks TO mmp_api")
    op.execute("GRANT INSERT ON skadnetwork_postbacks TO mmp_tracker")
    op.execute("GRANT SELECT, INSERT, UPDATE ON skadnetwork_postbacks TO mmp_worker")
    op.execute("GRANT SELECT ON skadnetwork_postbacks TO mmp_readonly")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS skadnetwork_postbacks")
    op.drop_index("ix_apps_apple_app_id", table_name="apps")
    op.drop_column("apps", "apple_app_id")
