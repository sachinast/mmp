"""Partition creation, routing and retention."""

from __future__ import annotations

import datetime as dt

import pytest
from mmp_core.ids import uuid7
from mmp_db.maintenance import (
    MINIMUM_RETENTION_DAYS,
    drop_expired_partitions,
    ensure_partitions,
    existing_partitions,
)
from mmp_db.partitions import PartitionSpec, create_partition_sql, partitions_for_range


def test_partition_naming_is_stable():
    spec = PartitionSpec("events", dt.date(2026, 9, 7))
    assert spec.name == "events_20260907"
    assert spec.end == dt.date(2026, 9, 8)


def test_unknown_table_is_rejected():
    """A typo must not silently create an unindexed partition of nothing."""
    with pytest.raises(ValueError, match="unknown partitioned table"):
        create_partition_sql(PartitionSpec("evnets", dt.date(2026, 9, 7)))


def test_range_covers_requested_days():
    specs = partitions_for_range("clicks", dt.date(2026, 9, 7), 3)
    assert [s.name for s in specs] == ["clicks_20260907", "clicks_20260908", "clicks_20260909"]


async def test_rows_route_to_the_right_partition(owner_conn):
    app_id, org_id = uuid7(), uuid7()
    today = dt.datetime.now(dt.UTC)
    event_id = uuid7()

    await owner_conn.execute(
        """INSERT INTO events (event_id, received_at, occurred_at, organization_id,
                               app_id, event_name, anonymous_id)
           VALUES ($1, $2, $2, $3, $4, 'install', 'anon-1')""",
        event_id,
        today,
        org_id,
        app_id,
    )
    try:
        partition = await owner_conn.fetchval(
            "SELECT tableoid::regclass::text FROM events WHERE event_id = $1", event_id
        )
        assert partition == f"events_{today:%Y%m%d}"
    finally:
        await owner_conn.execute("DELETE FROM events WHERE event_id = $1", event_id)


async def test_duplicate_event_id_is_rejected(owner_conn):
    """The dedup guarantee: at-least-once delivery, exactly-once counting."""
    app_id, org_id = uuid7(), uuid7()
    now = dt.datetime.now(dt.UTC)
    event_id = uuid7()
    insert = """INSERT INTO events (event_id, received_at, occurred_at, organization_id,
                                    app_id, event_name, anonymous_id)
                VALUES ($1, $2, $2, $3, $4, 'purchase', 'anon-1')"""
    await owner_conn.execute(insert, event_id, now, org_id, app_id)
    try:
        inserted = await owner_conn.execute(
            insert + " ON CONFLICT DO NOTHING", event_id, now, org_id, app_id
        )
        assert inserted == "INSERT 0 0", "a redelivered event must not be counted twice"
        count = await owner_conn.fetchval(
            "SELECT count(*) FROM events WHERE event_id = $1", event_id
        )
        assert count == 1
    finally:
        await owner_conn.execute("DELETE FROM events WHERE event_id = $1", event_id)


async def test_ensure_partitions_is_idempotent(owner_conn):
    today = dt.datetime.now(dt.UTC).date()
    first = await ensure_partitions(owner_conn, today=today, lead_days=2)
    second = await ensure_partitions(owner_conn, today=today, lead_days=2)
    assert first == second
    live = await existing_partitions(owner_conn, "events")
    assert f"events_{today:%Y%m%d}" in live


async def test_retention_floor_cannot_be_bypassed(owner_conn):
    """A misconfigured retention value must fail loudly, not delete data."""
    with pytest.raises(ValueError, match="below the"):
        await drop_expired_partitions(
            owner_conn, retention_days=MINIMUM_RETENTION_DAYS - 1, dry_run=False
        )


async def test_drop_defaults_to_dry_run(owner_conn):
    old_day = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=400)
    spec = PartitionSpec("events", old_day)
    for statement in create_partition_sql(spec):
        await owner_conn.execute(statement)
    try:
        planned = await drop_expired_partitions(owner_conn, retention_days=90)
        assert spec.name in planned
        assert spec.name in await existing_partitions(owner_conn, "events"), (
            "dry_run must not actually drop anything"
        )

        dropped = await drop_expired_partitions(owner_conn, retention_days=90, dry_run=False)
        assert spec.name in dropped
        assert spec.name not in await existing_partitions(owner_conn, "events")
    finally:
        await owner_conn.execute(f"DROP TABLE IF EXISTS {spec.name}")
