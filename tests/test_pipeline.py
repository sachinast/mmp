"""End to end: HTTP request in, row in Postgres, exactly once.

These are the tests that decide whether the phase is done. The endpoint tests
prove the edge behaves; these prove the *pipeline* does — including under the
failures it is built to survive.
"""

from __future__ import annotations

import asyncio

from tests.conftest_ingest import flush_tracker, sample_event


async def _drain(consumer, *, rounds: int = 5) -> None:
    for _ in range(rounds):
        if await consumer.run_once() == 0:
            break


async def test_event_reaches_postgres(tracker, worker_consumer, owner_conn, seeded_app):
    event = sample_event(event_name="purchase", revenue_minor=1999, currency="usd")
    response = await tracker.post("/v1/events", json={"events": [event]})
    assert response.status_code == 202

    await flush_tracker(tracker)
    await _drain(worker_consumer)

    row = await owner_conn.fetchrow(
        "SELECT event_id, event_name, revenue_minor, currency, organization_id "
        "FROM events WHERE app_id = $1",
        seeded_app["app_id"],
    )
    assert row is not None
    assert str(row["event_id"]) == event["event_id"]
    assert row["event_name"] == "purchase"
    assert row["revenue_minor"] == 1999
    # Normalised on the way in, so reports never split USD from usd.
    assert row["currency"] == "USD"
    assert row["organization_id"] == seeded_app["organization_id"]


async def test_batch_of_many_events(tracker, worker_consumer, owner_conn, seeded_app):
    events = [sample_event(event_name=f"custom_{i}") for i in range(50)]
    assert (await tracker.post("/v1/events", json={"events": events})).status_code == 202

    await flush_tracker(tracker)
    await _drain(worker_consumer)

    count = await owner_conn.fetchval(
        "SELECT count(*) FROM events WHERE app_id = $1", seeded_app["app_id"]
    )
    assert count == 50


async def test_stream_redelivery_is_deduplicated_exactly(
    tracker, worker_consumer, ingest_redis, owner_conn, seeded_app
):
    """The core exactly-once claim.

    Simulates a worker crashing after the write but before the acknowledgement:
    the same messages are handed to the consumer a second time. Because
    received_at is stamped at the edge and carried in the message, the redelivery
    reproduces the exact primary key and the database drops it.
    """
    events = [sample_event() for _ in range(10)]
    await tracker.post("/v1/events", json={"events": events})
    await flush_tracker(tracker)

    from mmp_ingest.stream import EVENTS_GROUP, EVENTS_STREAM

    first = await worker_consumer._consumer.read(count=100)
    assert len(first) == 10
    written = await worker_consumer._process(first)
    assert written == 10

    # Replay the same messages: reset the group cursor so they are redelivered.
    await ingest_redis.xgroup_setid(EVENTS_STREAM, EVENTS_GROUP, id="0")
    second = await worker_consumer._consumer.read(count=100)
    assert len(second) == 10, "the messages should be redelivered"
    await worker_consumer._process(second)

    count = await owner_conn.fetchval(
        "SELECT count(*) FROM events WHERE app_id = $1", seeded_app["app_id"]
    )
    assert count == 10, "a redelivered batch must not be counted twice"
    assert worker_consumer.metrics.duplicates == 10


async def test_events_are_not_acknowledged_before_they_are_written(
    tracker, worker_consumer, ingest_redis, seeded_app
):
    """Ack-after-commit, asserted directly.

    If the write fails, the messages must remain pending so another worker
    retries them. Acknowledging first would make any crash between ack and
    commit a silent loss.
    """
    from mmp_ingest.stream import EVENTS_GROUP, EVENTS_STREAM

    await tracker.post("/v1/events", json={"events": [sample_event() for _ in range(3)]})
    await flush_tracker(tracker)

    messages = await worker_consumer._consumer.read(count=10)
    assert len(messages) == 3

    async def failing_write(*_args, **_kwargs):
        raise RuntimeError("database is unavailable")

    worker_consumer._writer.write = failing_write
    await worker_consumer._process(messages)

    pending = await ingest_redis.xpending(EVENTS_STREAM, EVENTS_GROUP)
    count = pending["pending"] if isinstance(pending, dict) else pending[0]
    assert count == 3, "a failed batch must stay pending, not be acknowledged"


async def test_poison_messages_are_dead_lettered(
    tracker, worker_consumer, ingest_redis, seeded_app
):
    """One undigestible row must not stall every event behind it."""
    from mmp_worker.consumers import DEAD_LETTER_STREAM, MAX_ATTEMPTS

    await tracker.post("/v1/events", json={"events": [sample_event()]})
    await flush_tracker(tracker)

    async def failing_write(*_args, **_kwargs):
        raise RuntimeError("permanently undigestible")

    worker_consumer._writer.write = failing_write

    from mmp_ingest.stream import EVENTS_GROUP, EVENTS_STREAM

    for _ in range(MAX_ATTEMPTS):
        messages = await worker_consumer._consumer.read(count=10)
        if not messages:
            await ingest_redis.xgroup_setid(EVENTS_STREAM, EVENTS_GROUP, id="0")
            messages = await worker_consumer._consumer.read(count=10)
        if messages:
            await worker_consumer._process(messages)

    assert worker_consumer.metrics.dead_lettered >= 1
    assert await ingest_redis.xlen(DEAD_LETTER_STREAM) >= 1


async def test_stalled_messages_are_reclaimed(
    tracker, worker_consumer, ingest_redis, seeded_app, owner_conn
):
    """A worker killed mid-batch leaves messages delivered-but-unacknowledged.

    They are invisible to the ">" cursor, so without the reclaim sweep they
    would sit in Redis forever and appear in no report.
    """
    await tracker.post("/v1/events", json={"events": [sample_event() for _ in range(4)]})
    await flush_tracker(tracker)

    from mmp_ingest.stream import EVENTS_GROUP, EVENTS_STREAM

    # A different consumer reads them and then "dies" without acknowledging.
    await ingest_redis.xreadgroup(EVENTS_GROUP, "dead-worker", {EVENTS_STREAM: ">"}, count=10)
    assert await worker_consumer.run_once() == 0, "nothing new should be readable"

    claimed = await worker_consumer._consumer.claim_stalled(min_idle_ms=0, count=100)
    assert len(claimed) == 4
    await worker_consumer._process(claimed)

    count = await owner_conn.fetchval(
        "SELECT count(*) FROM events WHERE app_id = $1", seeded_app["app_id"]
    )
    assert count == 4


async def test_buffer_drains_on_shutdown(seeded_app, ingest_redis):
    """A rolling deploy must not discard accepted events.

    Without the final flush, every instance drops up to a full buffer of events
    it has already told the SDK were accepted.
    """
    import httpx
    from mmp_ingest.stream import EVENTS_STREAM
    from mmp_tracker.app import create_app

    app = create_app(seeded_app["tracker_settings"])
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            client.headers["authorization"] = f"Bearer {seeded_app['api_key']}"
            response = await client.post(
                "/v1/events", json={"events": [sample_event() for _ in range(5)]}
            )
            assert response.json()["accepted"] == 5
        # Exiting the lifespan runs the shutdown path — that is the assertion.

    assert await ingest_redis.xlen(EVENTS_STREAM) == 5, "accepted events were lost on shutdown"


async def test_buffer_sheds_oldest_and_counts_the_loss(seeded_app):
    """Shedding must be visible. A silent drop is worse than a counted one."""
    from mmp_ingest.stream import EVENTS_STREAM, StreamProducer
    from mmp_tracker.buffer import ShippingBuffer

    class NeverFlushes(StreamProducer):
        async def publish(self, payloads):  # type: ignore[override]
            raise RuntimeError("redis is unavailable")

    buffer = ShippingBuffer(NeverFlushes(None, stream=EVENTS_STREAM), capacity=10, flush_size=100)
    dropped = 0
    for index in range(25):
        dropped += buffer.append([f"event-{index}"])  # type: ignore[list-item]

    assert buffer.depth == 10
    assert dropped == 15
    assert buffer.dropped == 15
    # The newest data is what survives: under sustained overload the oldest
    # events are also the closest to being stale.
    assert "event-24" in list(buffer._queue)
    assert "event-0" not in list(buffer._queue)


async def test_redis_outage_degrades_rather_than_fails(seeded_app):
    """A queue outage must not become the customer's outage.

    The handler never awaits Redis, so ingestion keeps returning 202 while the
    buffer holds the backlog. Events are retained and shipped when Redis returns.
    """
    from mmp_ingest.stream import EVENTS_STREAM, StreamProducer
    from mmp_tracker.buffer import ShippingBuffer

    attempts = {"count": 0}

    class FlakyProducer(StreamProducer):
        async def publish(self, payloads):  # type: ignore[override]
            attempts["count"] += 1
            if attempts["count"] <= 2:
                raise ConnectionError("redis is down")
            return len(payloads)

    buffer = ShippingBuffer(
        FlakyProducer(None, stream=EVENTS_STREAM), capacity=100, flush_size=1000
    )
    buffer.append(["a", "b", "c"])  # type: ignore[list-item]

    await buffer._flush_once()
    assert buffer.depth == 3, "a failed flush must return events to the buffer, in order"
    assert buffer.failed_flushes == 1

    await buffer._flush_once()
    await buffer._flush_once()
    assert buffer.depth == 0
    assert buffer.shipped == 3


async def test_usage_metering_records_billable_volume(
    tracker, worker_consumer, owner_conn, seeded_app
):
    """Metering data cannot be reconstructed retroactively.

    You can only start counting from the day you decide to, which is why this
    exists in Phase 3 rather than whenever billing gets built.
    """
    from mmp_db.pool import Database
    from mmp_worker.jobs import refresh_usage

    await tracker.post("/v1/events", json={"events": [sample_event() for _ in range(7)]})
    await flush_tracker(tracker)
    await _drain(worker_consumer)

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        await refresh_usage(database)
    finally:
        await database.close()

    total = await owner_conn.fetchval(
        "SELECT sum(count) FROM usage_rollup WHERE app_id = $1 AND metric = 'events'",
        seeded_app["app_id"],
    )
    assert total == 7


async def test_reconciliation_counts_both_ends(tracker, worker_consumer, owner_conn, seeded_app):
    """Silent loss is only detectable if both ends are counted."""
    from mmp_db.pool import Database
    from mmp_worker.jobs import refresh_usage

    await tracker.post("/v1/events", json={"events": [sample_event() for _ in range(3)]})
    await flush_tracker(tracker)
    await _drain(worker_consumer)

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        await refresh_usage(database)
    finally:
        await database.close()

    persisted = await owner_conn.fetchval(
        "SELECT sum(count) FROM pipeline_audit WHERE app_id = $1 AND stage = 'persisted'",
        seeded_app["app_id"],
    )
    assert persisted == 3


async def test_concurrent_batches_do_not_lose_events(
    tracker, worker_consumer, owner_conn, seeded_app
):
    """Ten simultaneous requests, no interleaving losses."""
    batches = [
        [sample_event(event_name=f"batch{batch}_{i}") for i in range(10)] for batch in range(10)
    ]
    responses = await asyncio.gather(
        *(tracker.post("/v1/events", json={"events": batch}) for batch in batches)
    )
    assert all(r.status_code == 202 for r in responses)
    assert sum(r.json()["accepted"] for r in responses) == 100

    await flush_tracker(tracker)
    await _drain(worker_consumer, rounds=10)

    count = await owner_conn.fetchval(
        "SELECT count(*) FROM events WHERE app_id = $1", seeded_app["app_id"]
    )
    assert count == 100
