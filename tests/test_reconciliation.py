"""Reconciliation: the only thing that makes silent loss visible.

Every other failure announces itself. Events accepted and never stored produce
no signal at all — the numbers are simply lower than they should be, and nobody
knows until an advertiser reconciles against their ad network.
"""

from __future__ import annotations

import datetime as dt

import pytest
from mmp_worker.reconcile import (
    DRIFT_TOLERANCE,
    MINIMUM_VOLUME,
    Discrepancy,
    reconcile,
)


def _gap(accepted: int, persisted: int) -> Discrepancy:
    return Discrepancy(
        app_id="app",
        bucket_hour=dt.datetime.now(dt.UTC),
        accepted=accepted,
        persisted=persisted,
    )


def test_a_perfect_hour_is_not_a_discrepancy():
    assert not _gap(10_000, 10_000).significant


def test_dedup_shaped_drift_is_tolerated():
    """The edge counts accepted requests; the writer drops redelivered and
    retried events. That difference is healthy and constant."""
    assert not _gap(10_000, 9_950).significant  # 0.5%


def test_real_loss_is_flagged():
    assert _gap(10_000, 8_000).significant


def test_a_small_sample_is_not_flagged():
    """A 50% rate on four events is two events. A job that paged for that would
    be turned off within a week, and then it catches nothing."""
    assert not _gap(4, 2).significant
    assert _gap(4, 2).rate == 0.5, "the rate is still reported"


def test_the_thresholds_are_both_required():
    assert not _gap(MINIMUM_VOLUME - 1, 0).significant, "volume floor applies"
    just_over = int(MINIMUM_VOLUME * (1 - DRIFT_TOLERANCE)) - 1
    assert _gap(MINIMUM_VOLUME, just_over).significant


def test_rate_of_an_empty_hour_is_zero_not_an_error():
    assert _gap(0, 0).rate == 0.0
    assert not _gap(0, 0).significant


async def test_reconcile_records_both_ends(seeded_app, owner_conn, ingest_redis):
    """End to end: counts from Redis and from the events table meet in
    pipeline_audit."""
    from mmp_core.ids import uuid7
    from mmp_db.pool import Database

    now = dt.datetime.now(dt.UTC)
    bucket = (now - dt.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)

    for index in range(5):
        await owner_conn.execute(
            """INSERT INTO events (event_id, received_at, occurred_at, organization_id,
                                   app_id, event_name, anonymous_id, platform)
               VALUES ($1, $2, $2, $3, $4, 'install', $5, 1)""",
            uuid7(),
            bucket + dt.timedelta(minutes=index),
            seeded_app["organization_id"],
            seeded_app["app_id"],
            f"recon-{index}",
        )

    # What the edge says it accepted, left where the tracker leaves it.
    from mmp_ingest.audit import AcceptedCounter

    counter = AcceptedCounter(ingest_redis)
    counter.record(str(seeded_app["app_id"]), 5, now=bucket)
    await counter.flush()

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        result = await reconcile(database, ingest_redis, now=now)
    finally:
        await database.close()

    rows = {
        row["stage"]: row["count"]
        for row in await owner_conn.fetch(
            "SELECT stage, count FROM pipeline_audit WHERE app_id = $1 AND bucket_hour = $2",
            seeded_app["app_id"],
            bucket,
        )
    }
    assert rows.get("accepted") == 5
    assert rows.get("persisted") == 5
    # Scoped to this app: reconcile checks every app, and pipeline_audit is
    # shared across the suite, so an unscoped assertion would pick up whatever a
    # sibling test happened to leave behind.
    mine = [d for d in result.discrepancies if d.app_id == seeded_app["app_id"]]
    assert not mine, "matching counts are not a discrepancy"


async def test_missing_events_are_detected(seeded_app, owner_conn, ingest_redis):
    """The failure this job exists for: the edge accepted more than reached
    storage."""
    from mmp_core.ids import uuid7
    from mmp_db.pool import Database
    from mmp_ingest.audit import AcceptedCounter

    now = dt.datetime.now(dt.UTC)
    bucket = (now - dt.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)

    # 200 accepted, 100 stored: half the hour vanished.
    for index in range(100):
        await owner_conn.execute(
            """INSERT INTO events (event_id, received_at, occurred_at, organization_id,
                                   app_id, event_name, anonymous_id, platform)
               VALUES ($1, $2, $2, $3, $4, 'install', $5, 1)""",
            uuid7(),
            bucket + dt.timedelta(seconds=index),
            seeded_app["organization_id"],
            seeded_app["app_id"],
            f"lost-{index}",
        )

    counter = AcceptedCounter(ingest_redis)
    counter.record(str(seeded_app["app_id"]), 200, now=bucket)
    await counter.flush()

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        result = await reconcile(database, ingest_redis, now=now)
    finally:
        await database.close()

    mine = [d for d in result.discrepancies if d.app_id == seeded_app["app_id"]]
    assert mine, "a 50% gap must be reported"
    worst = mine[0]
    assert worst.accepted == 200
    assert worst.persisted == 100
    assert worst.missing == 100
    assert worst.rate == pytest.approx(0.5)


async def test_the_settling_period_excludes_the_current_hour(seeded_app, owner_conn, ingest_redis):
    """A batch accepted at 10:59:59 may still be in a buffer, a stream, or a
    worker's open transaction. Comparing the hour that just ended reports the
    pipeline's own latency as loss."""
    from mmp_db.pool import Database
    from mmp_ingest.audit import AcceptedCounter

    now = dt.datetime.now(dt.UTC)
    current = now.replace(minute=0, second=0, microsecond=0)

    counter = AcceptedCounter(ingest_redis)
    counter.record(str(seeded_app["app_id"]), 5000, now=current)
    await counter.flush()

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        result = await reconcile(database, ingest_redis, now=now)
    finally:
        await database.close()

    current_hour = [d for d in result.discrepancies if d.bucket_hour == current]
    assert not current_hour, "the in-flight hour must not be compared"


async def test_counter_recovers_a_failed_flush(ingest_redis):
    """A count lost on a failed flush would make the job report loss it caused
    itself."""
    from mmp_ingest.audit import AcceptedCounter

    class Broken:
        def pipeline(self, transaction=False):
            raise ConnectionError("redis is unavailable")

    counter = AcceptedCounter(Broken())  # type: ignore[arg-type]
    counter.record("app-1", 42)
    assert await counter.flush() == 0

    # The count survived and goes out on the next attempt.
    counter._redis = ingest_redis
    assert await counter.flush() == 42
