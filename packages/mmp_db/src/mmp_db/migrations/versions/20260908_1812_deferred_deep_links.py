"""Deferred deep links: carry the destination across the install.

Someone taps a link for one product, does not have the app, goes to the store,
installs, and opens it. Without this they land on a home screen and the reason
they tapped is lost — which is the single most common way a paid install turns
into a person who leaves.

The destination is stored twice on purpose.

``clicks.deep_link`` is where it arrives, and it is the record of what the link
actually asked for. ``attributions.deep_link`` is a copy made by the attribution
worker from the click that won. The copy is not redundant: the SDK asks for its
destination moments after first launch, and resolving from the attribution is a
lookup on ``(app_id, install_key)`` — an index that already exists — whereas
resolving from the click means finding one click id across every daily partition
of the largest table in the system. The duplication buys the hot lookup.

It also means the destination follows the attribution: if a later, better signal
supersedes the attribution, the new row carries its own click's destination
rather than the previous one's.

``deep_links`` gains a tracker grant and a lookup policy. The table has existed
since the first schema migration and nothing has read it until now; the redirect
path needs it to turn a registered code into a destination without trusting
whatever the query string said.

Revision ID: f2a7c3e14b60
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2a7c3e14b60"
down_revision: str | None = "c1b4f0d97a52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("clicks", sa.Column("deep_link", sa.Text(), nullable=True))
    op.add_column("attributions", sa.Column("deep_link", sa.Text(), nullable=True))

    # Read-only, and narrow. The tracker resolves a registered code on the
    # redirect path; it has no reason to write here and cannot.
    op.execute("GRANT SELECT ON deep_links TO mmp_tracker")
    op.execute(
        """
        CREATE POLICY tracker_lookup ON deep_links
            FOR SELECT TO mmp_tracker
            USING (true)
        """
    )
    # The original schema made `code` globally unique, which is a tenancy bug
    # rather than a constraint: deep link codes are resolved within an app, so a
    # global unique index means the first advertiser to register "summer" stops
    # every other advertiser on the platform from using it. Nothing had read the
    # table until now, so nothing depended on the old rule.
    op.drop_constraint("uq_deep_links_code", "deep_links", type_="unique")

    # The redirect looks a code up per app: an advertiser's codes are unique to
    # them, and two advertisers may both want "summer".
    op.create_index(
        "ix_deep_links_app_code",
        "deep_links",
        ["app_id", "code"],
        unique=True,
    )

    # The tracker needs to answer "what was this device after" at first launch,
    # which means reading attributions — and the tracker is the service on the
    # public internet. So the privilege is cut down twice, in the database
    # rather than in the query, because a control that lives in a query is a
    # control that a future query can forget.
    #
    # Columns: no click_id, no user_id, no campaign, no fraud verdict. The
    # tracker cannot read them even by accident.
    op.execute(
        """
        GRANT SELECT (app_id, install_key, deep_link, superseded_by, installed_at)
        ON attributions TO mmp_tracker
        """
    )
    # Rows: only the last 48 hours. The endpoint enforces 24, and this is the
    # backstop — if that check were ever dropped or bypassed, the database still
    # refuses to hand the tracker an install history to walk backwards through.
    op.execute(
        """
        CREATE POLICY tracker_deferred_lookup ON attributions
            FOR SELECT TO mmp_tracker
            USING (installed_at >= now() - interval '48 hours')
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tracker_deferred_lookup ON attributions")
    op.execute("REVOKE ALL ON attributions FROM mmp_tracker")
    op.drop_index("ix_deep_links_app_code", table_name="deep_links")
    op.create_unique_constraint("uq_deep_links_code", "deep_links", ["code"])
    op.execute("DROP POLICY IF EXISTS tracker_lookup ON deep_links")
    op.execute("REVOKE SELECT ON deep_links FROM mmp_tracker")
    op.drop_column("attributions", "deep_link")
    op.drop_column("clicks", "deep_link")
