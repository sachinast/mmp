"""Rollup correctness: idempotence, late arrivals, and the numbers themselves."""

from __future__ import annotations

import datetime as dt

from tests.conftest_ingest import ANDROID_UA, flush_tracker, sample_event


async def _persist(tracker, *consumers, rounds: int = 5) -> None:
    await flush_tracker(tracker)
    for _ in range(rounds):
        moved = 0
        for consumer in consumers:
            moved += await consumer.run_once()
        if moved == 0:
            break


async def _worker_db(seeded_app):
    from mmp_db.pool import Database

    return await Database.connect(seeded_app["worker_settings"], role="mmp_worker")


async def test_events_roll_up_with_correct_totals(tracker, worker_consumer, owner_conn, seeded_app):
    from mmp_worker.rollups import refresh_trailing

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                *(sample_event(event_name="install", anonymous_id=f"d{i}") for i in range(3)),
                *(
                    sample_event(
                        event_name="purchase",
                        anonymous_id=f"d{i}",
                        revenue_minor=1000 + i,
                        currency="USD",
                    )
                    for i in range(3)
                ),
            ]
        },
    )
    await _persist(tracker, worker_consumer)

    database = await _worker_db(seeded_app)
    try:
        await refresh_trailing(database)
    finally:
        await database.close()

    rows = {
        row["event_name"]: row
        for row in await owner_conn.fetch(
            "SELECT event_name, event_count, unique_devices, revenue_minor "
            "FROM rollup_events_hourly WHERE app_id = $1",
            seeded_app["app_id"],
        )
    }
    assert rows["install"]["event_count"] == 3
    assert rows["install"]["unique_devices"] == 3
    assert rows["purchase"]["event_count"] == 3
    assert rows["purchase"]["revenue_minor"] == 1000 + 1001 + 1002


async def test_refresh_is_idempotent(tracker, worker_consumer, owner_conn, seeded_app):
    """Recomputed, not incremented.

    An incrementing counter would double these numbers on the second run — and
    the second run is not hypothetical: the trailing refresh and the nightly
    late-arrival pass cover overlapping windows by design.
    """
    from mmp_worker.rollups import refresh_trailing

    await tracker.post(
        "/v1/events",
        json={"events": [sample_event(event_name="install") for _ in range(5)]},
    )
    await _persist(tracker, worker_consumer)

    database = await _worker_db(seeded_app)
    try:
        for _ in range(4):
            await refresh_trailing(database)
    finally:
        await database.close()

    total = await owner_conn.fetchval(
        "SELECT sum(event_count) FROM rollup_events_hourly "
        "WHERE app_id = $1 AND event_name = 'install'",
        seeded_app["app_id"],
    )
    assert total == 5, "four refreshes must produce the same number as one"


async def test_late_arrivals_are_picked_up(tracker, worker_consumer, owner_conn, seeded_app):
    """The SDK's offline queue holds events for up to seven days.

    Without the late-arrival pass those events would be in the raw table and in
    no report — present in the data, absent from every number anyone looks at.
    """
    from mmp_core.ids import uuid7
    from mmp_worker.rollups import refresh_late_arrivals, refresh_trailing

    database = await _worker_db(seeded_app)
    try:
        await refresh_trailing(database)
        before = await owner_conn.fetchval(
            "SELECT coalesce(sum(event_count), 0) FROM rollup_events_hourly WHERE app_id = $1",
            seeded_app["app_id"],
        )

        # An event that *happened* four days ago and is *arriving* now: exactly
        # what a device coming back from a week offline sends. It lands in
        # today's partition (received_at) but belongs in a bucket four days old
        # (occurred_at), which is why the trailing window cannot see it.
        now = dt.datetime.now(dt.UTC)
        occurred = now - dt.timedelta(days=4)
        await owner_conn.execute(
            """INSERT INTO events (event_id, received_at, occurred_at, organization_id,
                                   app_id, event_name, anonymous_id, platform)
               VALUES ($1, $2, $3, $4, $5, 'install', 'late-device', 1)""",
            uuid7(),
            now,
            occurred,
            seeded_app["organization_id"],
            seeded_app["app_id"],
        )

        await refresh_trailing(database)
        after_trailing = await owner_conn.fetchval(
            "SELECT coalesce(sum(event_count), 0) FROM rollup_events_hourly WHERE app_id = $1",
            seeded_app["app_id"],
        )
        assert after_trailing == before, "the trailing window should not reach back 4 days"

        await refresh_late_arrivals(database)
        after_late = await owner_conn.fetchval(
            "SELECT coalesce(sum(event_count), 0) FROM rollup_events_hourly WHERE app_id = $1",
            seeded_app["app_id"],
        )
        assert after_late == before + 1, "the late-arrival pass must find it"

        # And it must be credited to the day it happened, not the day it arrived.
        bucket = await owner_conn.fetchval(
            "SELECT bucket_hour FROM rollup_events_hourly "
            "WHERE app_id = $1 AND bucket_hour < $2 ORDER BY bucket_hour LIMIT 1",
            seeded_app["app_id"],
            now - dt.timedelta(days=1),
        )
        assert bucket is not None, "the event belongs to its own day, not to today"
        assert bucket.date() == occurred.date()
    finally:
        await database.close()


async def test_clicks_roll_up_and_bots_are_separated(
    tracker, click_consumer, owner_conn, seeded_app
):
    """Bot clicks are counted and reported separately, not discarded — an
    advertiser needs to see the traffic quality they are paying for."""
    from mmp_worker.rollups import refresh_trailing

    for _ in range(4):
        await tracker.get(
            f"/c/{seeded_app['tracking_code']}",
            headers={"user-agent": ANDROID_UA},
            follow_redirects=False,
        )
    for _ in range(2):
        await tracker.get(
            f"/c/{seeded_app['tracking_code']}",
            headers={"user-agent": "curl/8.4.0"},
            follow_redirects=False,
        )
    await _persist(tracker, click_consumer)

    database = await _worker_db(seeded_app)
    try:
        await refresh_trailing(database)
    finally:
        await database.close()

    row = await owner_conn.fetchrow(
        "SELECT sum(click_count) AS clicks, sum(bot_count) AS bots "
        "FROM rollup_clicks_hourly WHERE app_id = $1",
        seeded_app["app_id"],
    )
    assert row["clicks"] == 6
    assert row["bots"] == 2


async def test_campaign_rollup_joins_clicks_installs_and_revenue(
    tracker, click_consumer, worker_consumer, attribution_consumer, owner_conn, seeded_app
):
    """The campaign performance table, which is the one that pays for the
    attribution join."""
    from urllib.parse import parse_qs, unquote, urlparse

    from mmp_worker.rollups import refresh_trailing

    response = await tracker.get(
        f"/c/{seeded_app['tracking_code']}",
        headers={"user-agent": ANDROID_UA},
        follow_redirects=False,
    )
    referrer = unquote(parse_qs(urlparse(response.headers["location"]).query)["referrer"][0])
    await _persist(tracker, click_consumer)

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="install",
                    anonymous_id="camp-dev",
                    properties={"install_referrer": referrer},
                )
            ]
        },
    )
    await _persist(tracker, worker_consumer, attribution_consumer)

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="purchase",
                    anonymous_id="camp-dev",
                    revenue_minor=2500,
                    currency="USD",
                )
            ]
        },
    )
    await _persist(tracker, worker_consumer, attribution_consumer)

    database = await _worker_db(seeded_app)
    try:
        await refresh_trailing(database)
    finally:
        await database.close()

    row = await owner_conn.fetchrow(
        "SELECT clicks, installs, revenue_minor, conversions FROM rollup_campaign_daily "
        "WHERE app_id = $1 AND campaign_id = $2",
        seeded_app["app_id"],
        seeded_app["campaign_id"],
    )
    assert row is not None, "the campaign should appear in the daily rollup"
    assert row["clicks"] == 1
    assert row["installs"] == 1
    assert row["revenue_minor"] == 2500, "revenue must be credited to the campaign"
    assert row["conversions"] == 1


async def test_unattached_clicks_use_the_sentinel_campaign(owner_conn, seeded_app):
    """A NULL campaign_id in the key would make every refresh insert a new row
    instead of updating one, because NULLs do not compare equal."""
    from mmp_core.ids import uuid7
    from mmp_db.rollups import NO_CAMPAIGN
    from mmp_worker.rollups import refresh_trailing

    now = dt.datetime.now(dt.UTC)
    for _ in range(2):
        await owner_conn.execute(
            """INSERT INTO clicks (click_id, clicked_at, organization_id, app_id,
                                   campaign_id, tracking_link_id, platform, is_bot)
               VALUES ($1, $2, $3, $4, NULL, $5, 1, false)""",
            uuid7(),
            now,
            seeded_app["organization_id"],
            seeded_app["app_id"],
            seeded_app["tracking_link_id"],
        )

    database = await _worker_db(seeded_app)
    try:
        await refresh_trailing(database)
        await refresh_trailing(database)
    finally:
        await database.close()

    rows = await owner_conn.fetch(
        "SELECT campaign_id, click_count FROM rollup_clicks_hourly "
        "WHERE app_id = $1 AND campaign_id = $2::uuid",
        seeded_app["app_id"],
        NO_CAMPAIGN,
    )
    assert len(rows) == 1, "two refreshes must not create two rows"
    assert rows[0]["click_count"] == 2
