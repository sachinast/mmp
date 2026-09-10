"""Record which master key version wrapped each stored secret.

Without it a webhook cannot survive a master key rotation. ``seal()`` returns
the version it used, the create path discarded it, and both readers passed a
hard-coded ``1`` — so a webhook created *after* a rotation had its data key
wrapped under the new master key and read back under the old one. AES-GCM
answers that with ``InvalidTag``, the sender abandons the delivery because an
unsigned webhook is one the receiver has no reason to trust, and every
conversion for that endpoint stops arriving.

``provider_integrations`` has carried ``key_version`` since it was written.
``webhooks`` and ``postback_rules`` did not — the second was found by a test
that compares every table holding a wrapped data key against the ones that
record its version, rather than by anything failing.

Existing rows default to 1, which is correct rather than convenient: nothing has
rotated yet, so every secret in the table was sealed under version 1.

Revision ID: c4f1e83b2a97
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4f1e83b2a97"
down_revision: str | None = "b3e6a20d7c14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SEALED_TABLES = ("webhooks", "postback_rules")


def upgrade() -> None:
    for table in SEALED_TABLES:
        op.add_column(
            table,
            sa.Column("key_version", sa.Integer(), nullable=False, server_default="1"),
        )


def downgrade() -> None:
    # Destructive by nature: dropping the column loses the record of which key
    # wrapped each secret, and after a rotation that is the difference between a
    # secret that can be opened and one that cannot.
    for table in SEALED_TABLES:
        op.drop_column(table, "key_version")
