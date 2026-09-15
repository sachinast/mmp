"""Indexes the live view reads through.

The live view polls every couple of seconds for what happened in the last few
minutes. Events and clicks already serve that cheaply — both are partitioned by
time and their primary keys lead with it. Attributions and postback deliveries
had no index on time at all, so each poll would have read an app's entire
attribution history to find the last fifteen minutes of it, once per open
dashboard tab, every two seconds.

Built CONCURRENTLY. A plain CREATE INDEX takes a lock that blocks writes, and
these are the tables the attribution worker and the postback sender write to
continuously; a migration that pauses attribution for as long as an index build
takes on a large table is an outage with a migration's name on it. CONCURRENTLY
cannot run inside a transaction, hence the autocommit block.

Revision ID: d8a3c5f19e62
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d8a3c5f19e62"
down_revision: str | None = "c4f1e83b2a97"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_attributions_app_attributed_at",
            "attributions",
            ["app_id", "attributed_at"],
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        op.create_index(
            "ix_postback_deliveries_rule_created_at",
            "postback_deliveries",
            ["postback_rule_id", "created_at"],
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_postback_deliveries_rule_created_at",
            table_name="postback_deliveries",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.drop_index(
            "ix_attributions_app_attributed_at",
            table_name="attributions",
            postgresql_concurrently=True,
            if_exists=True,
        )
