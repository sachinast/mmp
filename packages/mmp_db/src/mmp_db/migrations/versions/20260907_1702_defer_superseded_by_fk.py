"""Make attributions.superseded_by deferrable.

Re-attribution is a two-statement swap: point the existing row at its
replacement, then insert the replacement. The partial unique index on
(app_id, install_key) WHERE superseded_by IS NULL forbids the reverse order,
so with an immediately-checked foreign key the correction is impossible to
perform atomically. Deferring the check to commit makes the swap one unit.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "8afb3aec00cf"
down_revision: str | None = "514babc412b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE attributions DROP CONSTRAINT fk_attributions_superseded_by")
    op.execute(
        "ALTER TABLE attributions ADD CONSTRAINT fk_attributions_superseded_by "
        "FOREIGN KEY (superseded_by) REFERENCES attributions(id) "
        "ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE attributions DROP CONSTRAINT fk_attributions_superseded_by")
    op.execute(
        "ALTER TABLE attributions ADD CONSTRAINT fk_attributions_superseded_by "
        "FOREIGN KEY (superseded_by) REFERENCES attributions(id) ON DELETE SET NULL"
    )
