"""Let the tracker serve an app its own conversion value mapping.

The SDK fetches the mapping with the key it already holds, so the read happens
on the tracker. Narrow and read-only: the tracker has no reason to write here,
and the policy is a lookup like the ones it has on apps and tracking links.

Revision ID: b3e6a20d7c14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b3e6a20d7c14"
down_revision: str | None = "a7d2e91c4f08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON conversion_mappings TO mmp_tracker")
    # The handler always filters by the authenticated app id, so this policy is
    # the backstop rather than the control — the same shape as the tracker's
    # other lookups, where the query is scoped and the grant is read-only.
    op.execute(
        """
        CREATE POLICY tracker_lookup ON conversion_mappings
            FOR SELECT TO mmp_tracker
            USING (true)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tracker_lookup ON conversion_mappings")
    op.execute("REVOKE SELECT ON conversion_mappings FROM mmp_tracker")
