"""Make a failed postback replayable, and give sandbox deliveries a status.

**Retries.** A failed delivery stored only its URL, and the retry job re-sent
that URL with the rule's method and nothing else. So a retry dropped the rule's
custom headers, sent a JSON POST with an empty body, and lost an adapter's bearer
token — on exactly the attempts that matter, since a retry means the partner was
briefly unavailable. A failed attempt now stores the request it made: method and
body in the clear, like the URL already was, and headers sealed, because they
carry partner credentials. They are written only when an attempt fails and will
be retried, so a delivered postback never leaves a copy of a secret behind.

**Sandbox.** Sandbox rules posted to an internal echo endpoint that never
existed, so every sandbox delivery failed. They now make no network call at all
and are recorded as ``sandbox``, with the request that would have been sent.

Revision ID: f3c9a1e27d48
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f3c9a1e27d48"
down_revision: str | None = "e2b7d4a91c35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Inlined, not imported from the models: a migration that reads live metadata
# changes meaning when the model does.
OLD_STATUSES = ("pending", "in_flight", "delivered", "failed", "abandoned")
NEW_STATUSES = (*OLD_STATUSES, "sandbox")


def _status_check(statuses: tuple[str, ...]) -> str:
    return "status IN (" + ", ".join(f"'{s}'" for s in statuses) + ")"


def upgrade() -> None:
    for name, column_type in (
        ("request_method", sa.String(8)),
        ("request_body", postgresql.BYTEA()),
        ("headers_ciphertext", postgresql.BYTEA()),
        ("headers_nonce", postgresql.BYTEA()),
        ("wrapped_dek", postgresql.BYTEA()),
    ):
        op.add_column("postback_deliveries", sa.Column(name, column_type, nullable=True))
    op.add_column(
        "postback_deliveries",
        sa.Column("key_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.drop_constraint(
        op.f("ck_postback_deliveries_status_valid"), "postback_deliveries", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_postback_deliveries_status_valid"),
        "postback_deliveries",
        _status_check(NEW_STATUSES),
    )


def downgrade() -> None:
    op.execute("UPDATE postback_deliveries SET status = 'abandoned' WHERE status = 'sandbox'")
    op.drop_constraint(
        op.f("ck_postback_deliveries_status_valid"), "postback_deliveries", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_postback_deliveries_status_valid"),
        "postback_deliveries",
        _status_check(OLD_STATUSES),
    )
    for column in (
        "key_version",
        "wrapped_dek",
        "headers_nonce",
        "headers_ciphertext",
        "request_body",
        "request_method",
    ):
        op.drop_column("postback_deliveries", column)
