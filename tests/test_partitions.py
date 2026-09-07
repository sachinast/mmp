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


async def test_a_utc_day_lands_entirely_in_its_own_partition(owner_conn):
    """A partition must hold the day it is named for, everywhere.

    Regression test for a bug the calendar found rather than the suite: a bare
    date literal in a partition bound on a timestamptz column is resolved in the
    *session* time zone of whoever ran the DDL. Created from a machine set to
    Asia/Kolkata, every partition covered 18:30 UTC to 18:30 UTC — five and a
    half hours off its own name. Retention would have dropped the wrong slice of
    data, and partitions made by CI would not have matched partitions made by a
    developer.

    Asserted behaviourally rather than by reading the bound's text: Postgres
    renders that text in the reader's own time zone, so a string comparison
    would test rendering rather than semantics. Both edges of a UTC day must
    route to the same partition, and the session is deliberately set to a
    non-UTC zone to prove the routing does not depend on it.
    """
    import datetime as dt

    from mmp_core.ids import uuid7
    from mmp_db.maintenance import ensure_partitions

    await ensure_partitions(owner_conn, lead_days=1)
    await owner_conn.execute("SET TIME ZONE 'Asia/Kolkata'")
    try:
        day = dt.datetime.now(dt.UTC).date()
        expected = f"events_{day:%Y%m%d}"
        app_id, org_id = uuid7(), uuid7()

        edges = {
            "start of day": dt.datetime.combine(day, dt.time(0, 0, 1), tzinfo=dt.UTC),
            "end of day": dt.datetime.combine(day, dt.time(23, 59, 59), tzinfo=dt.UTC),
        }
        written: list[object] = []
        for label, moment in edges.items():
            event_id = uuid7()
            written.append(event_id)
            await owner_conn.execute(
                """INSERT INTO events (event_id, received_at, occurred_at,
                                       organization_id, app_id, event_name, anonymous_id)
                   VALUES ($1, $2, $2, $3, $4, 'install', 'anon-tz')""",
                event_id,
                moment,
                org_id,
                app_id,
            )
            partition = await owner_conn.fetchval(
                "SELECT tableoid::regclass::text FROM events WHERE event_id = $1", event_id
            )
            assert partition == expected, (
                f"{label} ({moment.isoformat()}) landed in {partition}, not {expected}"
            )

        for event_id in written:
            await owner_conn.execute("DELETE FROM events WHERE event_id = $1", event_id)
    finally:
        await owner_conn.execute("SET TIME ZONE 'UTC'")


async def test_midnight_utc_event_lands_in_the_named_partition(owner_conn):
    """The concrete consequence: a row at 00:05 UTC belongs to that UTC day."""
    import datetime as dt

    from mmp_core.ids import uuid7
    from mmp_db.maintenance import ensure_partitions

    await ensure_partitions(owner_conn, lead_days=1)
    today = dt.datetime.now(dt.UTC).date()
    just_after_midnight = dt.datetime.combine(today, dt.time(0, 5), tzinfo=dt.UTC)
    event_id, app_id, org_id = uuid7(), uuid7(), uuid7()

    await owner_conn.execute(
        """INSERT INTO events (event_id, received_at, occurred_at, organization_id,
                               app_id, event_name, anonymous_id)
           VALUES ($1, $2, $2, $3, $4, 'install', 'anon-tz')""",
        event_id,
        just_after_midnight,
        org_id,
        app_id,
    )
    try:
        partition = await owner_conn.fetchval(
            "SELECT tableoid::regclass::text FROM events WHERE event_id = $1", event_id
        )
        assert partition == f"events_{today:%Y%m%d}", (
            "an event at 00:05 UTC must land in that UTC day's partition"
        )
    finally:
        await owner_conn.execute("DELETE FROM events WHERE event_id = $1", event_id)
