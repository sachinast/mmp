"""Session boundaries, decided server-side."""

from __future__ import annotations

import asyncio
import datetime as dt

from mmp_ingest.sessions import SessionTracker

from tests.conftest_ingest import flush_tracker, sample_event


async def test_first_event_starts_a_session(ingest_redis):
    tracker = SessionTracker(ingest_redis)
    decision = await tracker.resolve(app_id="app-1", anonymous_id="dev-1", event_name="app_open")
    assert decision.started


async def test_second_event_joins_the_same_session(ingest_redis):
    tracker = SessionTracker(ingest_redis)
    first = await tracker.resolve(app_id="app-1", anonymous_id="dev-1", event_name="app_open")
    second = await tracker.resolve(app_id="app-1", anonymous_id="dev-1", event_name="purchase")
    assert second.session_id == first.session_id
    assert not second.started


async def test_a_gap_longer_than_the_timeout_starts_a_new_session(ingest_redis):
    tracker = SessionTracker(ingest_redis)
    first = await tracker.resolve(
        app_id="app-1",
        anonymous_id="dev-1",
        event_name="app_open",
        session_timeout=dt.timedelta(seconds=1),
    )
    await asyncio.sleep(1.2)
    second = await tracker.resolve(
        app_id="app-1",
        anonymous_id="dev-1",
        event_name="app_open",
        session_timeout=dt.timedelta(seconds=1),
    )
    assert second.session_id != first.session_id
    assert second.started


async def test_activity_extends_the_session(ingest_redis):
    """Expiry is what ends a session, so every engaged event must push it out."""
    tracker = SessionTracker(ingest_redis)
    first = await tracker.resolve(
        app_id="app-1",
        anonymous_id="dev-1",
        event_name="app_open",
        session_timeout=dt.timedelta(seconds=2),
    )
    await asyncio.sleep(1.2)
    await tracker.resolve(
        app_id="app-1",
        anonymous_id="dev-1",
        event_name="purchase",
        session_timeout=dt.timedelta(seconds=2),
    )
    await asyncio.sleep(1.2)
    third = await tracker.resolve(
        app_id="app-1",
        anonymous_id="dev-1",
        event_name="purchase",
        session_timeout=dt.timedelta(seconds=2),
    )
    assert third.session_id == first.session_id, "activity should have kept it alive"


async def test_background_events_do_not_extend_a_session(ingest_redis):
    """A silent push receipt is not engagement.

    Counting it would inflate session length, which is a metric advertisers
    optimise against.
    """
    tracker = SessionTracker(ingest_redis)
    await tracker.resolve(
        app_id="app-1",
        anonymous_id="dev-1",
        event_name="app_open",
        session_timeout=dt.timedelta(seconds=1),
    )
    await asyncio.sleep(0.6)
    await tracker.resolve(
        app_id="app-1",
        anonymous_id="dev-1",
        event_name="push_received",
        session_timeout=dt.timedelta(seconds=1),
    )
    await asyncio.sleep(0.6)
    assert await tracker.current(app_id="app-1", anonymous_id="dev-1") is None


async def test_devices_do_not_share_sessions(ingest_redis):
    tracker = SessionTracker(ingest_redis)
    a = await tracker.resolve(app_id="app-1", anonymous_id="dev-a", event_name="app_open")
    b = await tracker.resolve(app_id="app-1", anonymous_id="dev-b", event_name="app_open")
    assert a.session_id != b.session_id


async def test_apps_do_not_share_sessions(ingest_redis):
    tracker = SessionTracker(ingest_redis)
    a = await tracker.resolve(app_id="app-1", anonymous_id="dev-1", event_name="app_open")
    b = await tracker.resolve(app_id="app-2", anonymous_id="dev-1", event_name="app_open")
    assert a.session_id != b.session_id


async def test_concurrent_events_do_not_create_two_sessions(ingest_redis):
    """The race the SET NX exists to close.

    A GET-then-SET implementation lets two events from the same device each see
    no session and each create one — which is exactly what happens when an SDK
    flushes a batch after coming back online.
    """
    tracker = SessionTracker(ingest_redis)
    decisions = await asyncio.gather(
        *(
            tracker.resolve(app_id="app-1", anonymous_id="dev-race", event_name="app_open")
            for _ in range(10)
        )
    )
    assert len({d.session_id for d in decisions}) == 1
    assert sum(1 for d in decisions if d.started) == 1


async def test_session_end_closes_it(ingest_redis):
    tracker = SessionTracker(ingest_redis)
    started = await tracker.resolve(app_id="app-1", anonymous_id="dev-1", event_name="app_open")
    ended = await tracker.end(app_id="app-1", anonymous_id="dev-1")
    assert ended == started.session_id
    assert await tracker.current(app_id="app-1", anonymous_id="dev-1") is None


async def test_ingest_assigns_a_session_id(tracker, worker_consumer, owner_conn, seeded_app):
    """Every event reaches storage carrying the session it belonged to."""
    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(event_name="app_open", anonymous_id="sess-dev"),
                sample_event(
                    event_name="purchase",
                    anonymous_id="sess-dev",
                    revenue_minor=500,
                    currency="USD",
                ),
            ]
        },
    )
    await flush_tracker(tracker)
    for _ in range(3):
        if await worker_consumer.run_once() == 0:
            break

    rows = await owner_conn.fetch(
        "SELECT event_name, session_id FROM events WHERE app_id = $1 AND anonymous_id = 'sess-dev'",
        seeded_app["app_id"],
    )
    assert len(rows) == 2
    assert all(row["session_id"] is not None for row in rows)
    assert len({row["session_id"] for row in rows}) == 1, "one session, two events"


async def test_sdk_supplied_session_id_is_honoured(
    tracker, worker_consumer, owner_conn, seeded_app
):
    """A client that tracks its own foreground/background transitions knows
    things the server cannot see."""
    from mmp_core.ids import uuid7

    supplied = str(uuid7())
    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(event_name="app_open", anonymous_id="sdk-sess", session_id=supplied)
            ]
        },
    )
    await flush_tracker(tracker)
    for _ in range(3):
        if await worker_consumer.run_once() == 0:
            break

    stored = await owner_conn.fetchval(
        "SELECT session_id FROM events WHERE app_id = $1 AND anonymous_id = 'sdk-sess'",
        seeded_app["app_id"],
    )
    assert str(stored) == supplied


async def test_batched_resolution_matches_the_single_path(ingest_redis):
    """The fast path must give the same answers as the simple one.

    resolve_many exists purely for latency; if it ever disagrees with resolve,
    session counts would depend on how events happened to be batched.
    """
    tracker = SessionTracker(ingest_redis)

    first = await tracker.resolve_many(
        app_id="app-b", devices=[("dev-1", "app_open"), ("dev-2", "app_open")]
    )
    assert len(first) == 2
    assert all(d.started for d in first.values())
    assert first["dev-1"].session_id != first["dev-2"].session_id

    # A second pass adopts both rather than starting new ones.
    second = await tracker.resolve_many(
        app_id="app-b", devices=[("dev-1", "purchase"), ("dev-2", "purchase")]
    )
    assert not any(d.started for d in second.values())
    assert second["dev-1"].session_id == first["dev-1"].session_id
    assert second["dev-2"].session_id == first["dev-2"].session_id

    # And it agrees with the single-device path.
    single = await tracker.resolve(app_id="app-b", anonymous_id="dev-1", event_name="purchase")
    assert single.session_id == first["dev-1"].session_id


async def test_batched_resolution_handles_mixed_engagement(ingest_redis):
    """A batch can contain both engaged and background events."""
    tracker = SessionTracker(ingest_redis)
    decisions = await tracker.resolve_many(
        app_id="app-c",
        devices=[("dev-live", "app_open"), ("dev-quiet", "push_received")],
    )
    assert decisions["dev-live"].started
    assert not decisions["dev-quiet"].started
    # A background event alone must not have created a session.
    assert await tracker.current(app_id="app-c", anonymous_id="dev-quiet") is None


async def test_all_events_for_one_device_share_a_session(
    tracker, worker_consumer, owner_conn, seeded_app
):
    """The dedup in the batched path must not give two events two sessions."""
    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(event_name="app_open", anonymous_id="multi-dev"),
                sample_event(
                    event_name="purchase",
                    anonymous_id="multi-dev",
                    revenue_minor=100,
                    currency="USD",
                ),
                sample_event(event_name="custom_a", anonymous_id="multi-dev"),
                sample_event(event_name="app_open", anonymous_id="other-dev"),
            ]
        },
    )
    await flush_tracker(tracker)
    for _ in range(3):
        if await worker_consumer.run_once() == 0:
            break

    rows = await owner_conn.fetch(
        "SELECT anonymous_id, session_id FROM events WHERE app_id = $1 "
        "AND anonymous_id IN ('multi-dev', 'other-dev')",
        seeded_app["app_id"],
    )
    by_device: dict[str, set] = {}
    for row in rows:
        by_device.setdefault(row["anonymous_id"], set()).add(row["session_id"])

    assert len(by_device["multi-dev"]) == 1, "one device in one batch, one session"
    assert len(by_device["other-dev"]) == 1
    assert by_device["multi-dev"] != by_device["other-dev"]
