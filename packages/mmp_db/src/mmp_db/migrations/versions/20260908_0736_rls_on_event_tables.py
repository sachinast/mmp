"""Row-level security on the event tables.

Found by auditing the schema rather than by a failing test: ``events`` and
``clicks`` carry ``organization_id`` and had no RLS, while ``mmp_api`` held
SELECT on every table in the schema. Nothing queries them from the API today —
the dashboard reads rollups — but the event explorer is a planned feature, and
the first query someone writes for it would have returned every tenant's events.

The original reasoning for leaving them out was that they are not on a request
path. That was true when it was written and stopped being true the moment the
API role could reach them. A privilege that is only safe because nobody has used
it yet is not a control.

Measured before enabling: 5,000-row batches through the real COPY-then-INSERT
path took a median of 13.2 ms without RLS and 12.0 ms with it — no measurable
cost, because the policy is a constant-true check for the roles that write.

Policies, narrowest first:

* ``mmp_api`` gets tenant isolation — the same rule as every other table, so a
  future event-explorer query cannot cross a tenant even with no WHERE clause.
* ``mmp_tracker`` may INSERT without a tenant set. It authenticates *into* an
  organisation and writes on its behalf; requiring it to set a session variable
  per batch would put a round trip on the ingest path.
* ``mmp_worker`` crosses tenants by design.

Revision ID: 88313e020033'
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "88313e020033"
down_revision: str | None = "34493ebdaa7e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_TABLES = ("events", "clicks")


def upgrade() -> None:
    for table in EVENT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

        op.execute(
            f"""
            CREATE POLICY org_isolation ON {table}
                USING (
                    organization_id
                    = NULLIF(current_setting('mmp.org_id', true), '')::uuid
                )
                WITH CHECK (
                    organization_id
                    = NULLIF(current_setting('mmp.org_id', true), '')::uuid
                )
            """
        )
        # Write-only, and no USING clause: the tracker may add rows and cannot
        # read any back. It has no reason to read events, and not being able to
        # is worth more than the symmetry.
        op.execute(
            f"""
            CREATE POLICY tracker_insert ON {table}
                FOR INSERT TO mmp_tracker
                WITH CHECK (true)
            """
        )
        op.execute(
            f"""
            CREATE POLICY worker_access ON {table}
                TO mmp_worker
                USING (true) WITH CHECK (true)
            """
        )


def downgrade() -> None:
    for table in EVENT_TABLES:
        for policy in ("worker_access", "tracker_insert", "org_isolation"):
            op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
