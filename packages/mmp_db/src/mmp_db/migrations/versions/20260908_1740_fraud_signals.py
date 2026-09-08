"""Fraud signals: a verdict on each attribution, findings for each link.

Two shapes because there are two kinds of evidence, and giving them one table
would have been wrong in both directions.

Per-install signals — an implausible click-to-install gap, a bot click, a
replayed device — belong to exactly one attribution and are useless without it.
They live as columns on ``attributions``: no join to explain a number, and no
second table growing at the rate of the first. Under a flood attack a shared
signals table would take millions of rows a day for what is, per install, three
small values.

Per-link findings — flooding, click farms — are properties of a population, not
of any one install. One row per link per rule per window, so this table stays
small enough to scan and to show an advertiser directly. The unique constraint
makes re-running the job idempotent: the fraud sweep is exactly the kind of job
that gets re-run after a crash, and a re-run must not double a finding that
someone is about to be shown.

Attributions are immutable by design elsewhere — a re-attribution inserts a new
row rather than editing one. The fraud columns are the deliberate exception:
they are an assessment *of* the row, not part of what was attributed, and a
verdict that could never be corrected once written would mean a false positive
was permanent. Corrections are written by the sweep and are visible in the
audit log.

Nothing here changes an attribution or hides a row. The verdict is recorded
next to the install and every read path continues to return flagged rows unless
a caller asks otherwise, so the numbers cannot silently move under anyone.

Revision ID: c1b4f0d97a52
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c1b4f0d97a52"
down_revision: str | None = "3e7fcad0d909"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out rather than imported from mmp_attrib.fraud. A migration that reads
# live application code stops being a record of what the schema was and becomes
# a function of what the code is now — which broke a migration in this repo
# once already, when adding a model retroactively changed an applied one.
VERDICTS = ("clean", "suspicious", "fraudulent")


def upgrade() -> None:
    op.add_column(
        "attributions",
        sa.Column("fraud_score", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "attributions",
        sa.Column(
            "fraud_verdict",
            sa.String(length=16),
            nullable=False,
            server_default="clean",
        ),
    )
    # The rules that fired, so a support engineer can answer "why" without
    # re-running the assessment against data that may since have been erased.
    op.add_column(
        "attributions",
        sa.Column("fraud_rules", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        "fraud_verdict_valid",
        "attributions",
        sa.column("fraud_verdict").in_(VERDICTS),
    )
    # Partial: clean is the overwhelming majority and nobody queries for it, so
    # indexing it would cost writes on every install to serve no read.
    op.create_index(
        "ix_attributions_fraud_verdict",
        "attributions",
        ["app_id", "fraud_verdict"],
        postgresql_where=sa.text("fraud_verdict <> 'clean'"),
    )

    op.create_table(
        "fraud_findings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("app_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tracking_link_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rule", sa.String(length=40), nullable=False),
        sa.Column("severity", sa.SmallInteger(), nullable=False),
        # The sentence shown to a customer, stored rather than regenerated: the
        # thresholds it quotes may have changed by the time anyone reads it, and
        # the finding must say what it said when it was made.
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_fraud_findings_organization_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["app_id"], ["apps.id"], name=op.f("fk_fraud_findings_app_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fraud_findings")),
    )
    op.create_index(
        op.f("ix_fraud_findings_organization_id"),
        "fraud_findings",
        ["organization_id"],
    )
    op.create_index(
        "ix_fraud_findings_app_window",
        "fraud_findings",
        ["app_id", "window_start"],
    )
    # Idempotence for the sweep. NULLS NOT DISTINCT so that a finding with no
    # tracking link (an app-wide one) still collides with itself on a re-run —
    # without it, PostgreSQL treats every NULL as unique and the constraint
    # silently stops protecting exactly the rows it was added for.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_fraud_findings_window
            ON fraud_findings (app_id, tracking_link_id, rule, window_start)
            NULLS NOT DISTINCT
        """
    )

    op.execute("ALTER TABLE fraud_findings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE fraud_findings FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY org_isolation ON fraud_findings
            USING (
                organization_id = NULLIF(current_setting('mmp.org_id', true), '')::uuid
            )
            WITH CHECK (
                organization_id = NULLIF(current_setting('mmp.org_id', true), '')::uuid
            )
        """
    )
    # The sweep reads every tenant's traffic to score it, which is the one job
    # that has to cross tenants by design.
    op.execute(
        """
        CREATE POLICY worker_access ON fraud_findings
            TO mmp_worker USING (true) WITH CHECK (true)
        """
    )
    op.execute("GRANT SELECT ON fraud_findings TO mmp_api")
    op.execute("GRANT SELECT, INSERT, UPDATE ON fraud_findings TO mmp_worker")
    op.execute("GRANT SELECT ON fraud_findings TO mmp_readonly")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS fraud_findings")
    op.drop_index("ix_attributions_fraud_verdict", table_name="attributions")
    op.drop_constraint("fraud_verdict_valid", "attributions", type_="check")
    op.drop_column("attributions", "fraud_rules")
    op.drop_column("attributions", "fraud_verdict")
    op.drop_column("attributions", "fraud_score")
