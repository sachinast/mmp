"""Carry a partner's sub parameters through to postbacks, safely.

A partner running a campaign appends its own click id to the tracking link —
``?sub1={their_click_id}`` — and needs it back on the install postback to match
the install to its click. The click row has always stored ``sub1`` to ``sub3``; the
attribution did not, so nothing downstream could return them. A purchase days
after the install is resolved through the attribution, not the click, so the
values are copied onto the attribution when the install is matched: an
attribution is the immutable record of which click earned an install, and what
that click carried is part of that record.

``postback_rules.campaign_id`` scopes a rule to one campaign. Rules used to fire
for every install of their app whichever partner earned it, so two partners each
with a rule were told about each other's installs; returning ``sub1`` on top of
that would hand one partner's click ids to another. A rule that references a sub
parameter is refused unless it is scoped — enforced in the API, where the error
can say why.

ON DELETE CASCADE rather than SET NULL: a scoped rule whose campaign vanished
must not quietly become an app-wide rule and start receiving every partner's
installs. Campaigns are not deletable through the API today; this decides what
happens if that changes.

Revision ID: e2b7d4a91c35
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e2b7d4a91c35"
down_revision: str | None = "d8a3c5f19e62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SUB_PARAMS = ("sub1", "sub2", "sub3")


def upgrade() -> None:
    for column in SUB_PARAMS:
        op.add_column("attributions", sa.Column(column, sa.Text(), nullable=True))

    op.add_column(
        "postback_rules",
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_postback_rules_campaign_id_campaigns",
        "postback_rules",
        "campaigns",
        ["campaign_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_postback_rules_campaign_id", "postback_rules", ["campaign_id"])


def downgrade() -> None:
    op.drop_index("ix_postback_rules_campaign_id", table_name="postback_rules")
    op.drop_constraint(
        "fk_postback_rules_campaign_id_campaigns", "postback_rules", type_="foreignkey"
    )
    op.drop_column("postback_rules", "campaign_id")
    for column in reversed(SUB_PARAMS):
        op.drop_column("attributions", column)
