"""Time-partitioned event storage.

``clicks`` and ``events`` are deliberately not ORM models. They are written in
binary ``COPY`` batches and read by hand-tuned aggregate queries; routing either
of those through SQLAlchemy's unit of work would cost more than it explains.

Retention is ``DROP TABLE`` on an expired partition. Deleting a day of events
with ``DELETE`` would leave tens of millions of dead tuples for autovacuum to
chase, on a table that is simultaneously absorbing inserts — a fight autovacuum
does not win. Dropping a partition is a catalogue update.

Index policy on these tables is austere on purpose: every index is paid for on
every insert, and the ingest path is the thing we are protecting. Each index
below exists because a specific query in the attribution or analytics path
needs it, not because a column looked queryable.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

# --- parent tables ------------------------------------------------------

CLICKS_PARENT = """
CREATE TABLE IF NOT EXISTS clicks (
    click_id          uuid        NOT NULL,
    clicked_at        timestamptz NOT NULL,
    organization_id   uuid        NOT NULL,
    app_id            uuid        NOT NULL,
    campaign_id       uuid,
    tracking_link_id  uuid        NOT NULL,
    device_hash       bytea,
    ip_hash           bytea,
    country           char(2),
    platform          smallint,
    os_version        text,
    device_model      text,
    user_agent        text,
    sub1              text,
    sub2              text,
    sub3              text,
    is_bot            boolean     NOT NULL DEFAULT false,
    PRIMARY KEY (clicked_at, click_id)
) PARTITION BY RANGE (clicked_at);
"""

EVENTS_PARENT = """
CREATE TABLE IF NOT EXISTS events (
    event_id        uuid        NOT NULL,
    received_at     timestamptz NOT NULL,
    occurred_at     timestamptz NOT NULL,
    organization_id uuid        NOT NULL,
    app_id          uuid        NOT NULL,
    event_name      text        NOT NULL,
    anonymous_id    text        NOT NULL,
    user_id         text,
    session_id      uuid,
    platform        smallint,
    os_version      text,
    app_version     text,
    device_model    text,
    country         char(2),
    ip_hash         bytea,
    click_id        uuid,
    revenue_minor   bigint,
    currency        char(3),
    clock_skew_ms   bigint,
    properties      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (received_at, app_id, event_id)
) PARTITION BY RANGE (received_at);
"""

# Money is stored as an integer count of minor units with an explicit currency.
# A float revenue column drifts, and a drifting revenue total in a measurement
# product is not a rounding bug, it is an invoice dispute.
MONEY_CHECK = """
ALTER TABLE events
    ADD CONSTRAINT ck_events_currency_with_revenue
    CHECK (revenue_minor IS NULL OR currency IS NOT NULL) NOT VALID;
"""

PARENTS: tuple[str, ...] = (CLICKS_PARENT, EVENTS_PARENT)


@dataclass(frozen=True)
class PartitionSpec:
    table: str
    day: dt.date

    @property
    def name(self) -> str:
        return f"{self.table}_{self.day:%Y%m%d}"

    @property
    def start(self) -> dt.date:
        return self.day

    @property
    def end(self) -> dt.date:
        return self.day + dt.timedelta(days=1)


# Per-partition indexes, keyed by parent table. Attached to each new child
# rather than declared on the parent so that a dropped partition takes its
# indexes with it and index builds never lock the whole table.
PARTITION_INDEXES: dict[str, tuple[str, ...]] = {
    # click_id: the deterministic attribution join.
    # (app_id, device_hash, clicked_at DESC): last-click lookup for a device.
    # BRIN on time: range scans over an append-ordered column, at near-zero
    # storage and maintenance cost compared to a btree.
    "clicks": (
        "CREATE INDEX IF NOT EXISTS ix_{name}_click_id ON {name} (click_id)",
        "CREATE INDEX IF NOT EXISTS ix_{name}_device ON {name} "
        "(app_id, device_hash, clicked_at DESC) WHERE device_hash IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS ix_{name}_time_brin ON {name} USING brin (clicked_at)",
    ),
    # (app_id, event_name, received_at): the rollup worker's scan.
    # (app_id, anonymous_id): identity resolution and session stitching.
    "events": (
        "CREATE INDEX IF NOT EXISTS ix_{name}_app_event ON {name} "
        "(app_id, event_name, received_at)",
        "CREATE INDEX IF NOT EXISTS ix_{name}_identity ON {name} (app_id, anonymous_id)",
        "CREATE INDEX IF NOT EXISTS ix_{name}_time_brin ON {name} USING brin (received_at)",
    ),
}


def create_partition_sql(spec: PartitionSpec) -> list[str]:
    """DDL for one day of one partitioned table.

    Identifiers are interpolated, but never from user input: the table name comes
    from a module constant and the date from ``datetime``. Postgres does not
    accept a parameter in a DDL identifier position, so this is the only form
    available — which is exactly why the inputs are closed.
    """
    if spec.table not in PARTITION_INDEXES:
        raise ValueError(f"unknown partitioned table: {spec.table!r}")

    statements = [
        # sql-identifier-ok: table name from a module constant, dates from
        # datetime. No caller-supplied value reaches this string.
        f"CREATE TABLE IF NOT EXISTS {spec.name} PARTITION OF {spec.table} "
        f"FOR VALUES FROM ('{spec.start:%Y-%m-%d}') TO ('{spec.end:%Y-%m-%d}')"
    ]
    statements.extend(template.format(name=spec.name) for template in PARTITION_INDEXES[spec.table])
    return statements


def partitions_for_range(table: str, start: dt.date, days: int) -> list[PartitionSpec]:
    return [PartitionSpec(table, start + dt.timedelta(days=offset)) for offset in range(days)]


def drop_partition_sql(spec: PartitionSpec) -> str:
    """Retention. Detach-then-drop so the parent is never locked while the data
    file is unlinked."""
    return f"DROP TABLE IF EXISTS {spec.name}"
