"""The ingest endpoint: authentication, limits, validation, acceptance."""

from __future__ import annotations

import gzip

import msgspec
import pytest

from tests.conftest_ingest import sample_event


async def test_accepts_a_batch(tracker):
    response = await tracker.post("/v1/events", json={"events": [sample_event()]})
    assert response.status_code == 202, response.text
    assert response.json() == {"accepted": 1, "duplicates": 0, "dropped": 0}


async def test_returns_before_the_write_happens(tracker, owner_conn, seeded_app):
    """202 Accepted, not 201 Created.

    The status code is a promise about durability, and ours is 'queued', not
    'stored'. Anything stronger would require the write to be synchronous, which
    is the design this whole service exists to avoid.
    """
    response = await tracker.post("/v1/events", json={"events": [sample_event()]})
    assert response.status_code == 202
    stored = await owner_conn.fetchval(
        "SELECT count(*) FROM events WHERE app_id = $1", seeded_app["app_id"]
    )
    assert stored == 0, "the endpoint must not write synchronously"


async def test_missing_credentials_rejected(tracker):
    response = await tracker.post(
        "/v1/events", json={"events": [sample_event()]}, headers={"authorization": ""}
    )
    assert response.status_code == 401


async def test_bad_key_is_indistinguishable_from_unknown_key(tracker, seeded_app):
    """No probing which prefixes are real."""
    real = seeded_app["api_key"]
    tampered = real[:-4] + "aaaa"
    unknown = "mmp_live_" + "a" * 12 + "_" + "b" * 43

    first = await tracker.post(
        "/v1/events",
        json={"events": [sample_event()]},
        headers={"authorization": f"Bearer {tampered}"},
    )
    second = await tracker.post(
        "/v1/events",
        json={"events": [sample_event()]},
        headers={"authorization": f"Bearer {unknown}"},
    )
    assert first.status_code == second.status_code == 401
    assert first.json() == second.json()


async def test_revoked_key_stops_working_immediately(tracker, owner_conn, seeded_app):
    """Revocation must not wait for a cache TTL."""
    from mmp_crypto.keys import api_key_cache_key, parse_key

    assert (await tracker.post("/v1/events", json={"events": [sample_event()]})).status_code == 202

    prefix = parse_key(seeded_app["api_key"]).prefix
    await owner_conn.execute("UPDATE api_keys SET status = 'revoked' WHERE key_prefix = $1", prefix)
    state = tracker.tracker_app.state.tracker
    await state.redis.delete(api_key_cache_key(prefix))

    after = await tracker.post("/v1/events", json={"events": [sample_event()]})
    assert after.status_code == 401


async def test_disabled_app_stops_accepting(tracker, owner_conn, seeded_app):
    from mmp_crypto.keys import api_key_cache_key, parse_key

    await owner_conn.execute(
        "UPDATE apps SET status = 'disabled' WHERE id = $1", seeded_app["app_id"]
    )
    prefix = parse_key(seeded_app["api_key"]).prefix
    await tracker.tracker_app.state.tracker.redis.delete(api_key_cache_key(prefix))

    response = await tracker.post("/v1/events", json={"events": [sample_event()]})
    assert response.status_code == 401


async def test_oversized_batch_rejected(tracker):
    from mmp_ingest.schema import MAX_EVENTS_PER_BATCH

    events = [sample_event() for _ in range(MAX_EVENTS_PER_BATCH + 1)]
    response = await tracker.post("/v1/events", json={"events": events})
    assert response.status_code == 413


async def test_oversized_payload_rejected(tracker):
    """Content-Length is a claim; the cap is enforced while reading."""
    fat = sample_event(properties={"blob": "x" * 300_000})
    response = await tracker.post("/v1/events", json={"events": [fat]})
    assert response.status_code == 413


async def test_decompression_bomb_rejected(tracker):
    """A few hundred KB of gzip can expand to gigabytes.

    Without a bound on the decompressed size, this endpoint would hand an
    unauthenticated caller a memory-exhaustion primitive.
    """
    payload = msgspec.json.encode({"events": [sample_event(properties={"pad": "a" * 200_000})]})
    compressed = gzip.compress(payload * 40)
    assert len(compressed) < 250_000, "the test's own bomb must fit under the size cap"

    response = await tracker.post(
        "/v1/events",
        content=compressed,
        headers={"content-encoding": "gzip", "content-type": "application/json"},
    )
    assert response.status_code == 413


async def test_malformed_json_rejected(tracker):
    response = await tracker.post(
        "/v1/events", content=b"{not json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"event_name": ""}, "empty event name"),
        ({"event_name": "x" * 200}, "unbounded name explodes rollup cardinality"),
        ({"anonymous_id": ""}, "no identity to attribute to"),
        ({"event_id": "not-a-uuid"}, "idempotency anchor must be a UUID"),
        ({"revenue_minor": 500}, "revenue without a currency is meaningless"),
        ({"revenue_minor": 500, "currency": "DOLLARS"}, "currency must be ISO"),
        ({"occurred_at": "yesterday"}, "unparseable timestamp"),
        ({"click_id": "nope"}, "click_id must be a UUID"),
    ],
)
async def test_invalid_events_rejected(tracker, overrides, reason):
    response = await tracker.post("/v1/events", json={"events": [sample_event(**overrides)]})
    assert response.status_code == 422, f"should reject: {reason}"


async def test_empty_batch_is_accepted_not_an_error(tracker):
    """An SDK flushing an empty queue is normal, not a client error."""
    response = await tracker.post("/v1/events", json={"events": []})
    assert response.status_code == 202
    assert response.json()["accepted"] == 0


async def test_server_mints_event_id_when_absent(tracker, ingest_redis):
    """S2S callers may not supply one; they still get idempotency downstream."""
    event = sample_event()
    del event["event_id"]
    response = await tracker.post("/v1/events", json={"events": [event]})
    assert response.status_code == 202
    assert response.json()["accepted"] == 1


async def test_client_retry_is_deduplicated_at_the_edge(tracker):
    """The SDK removes an event only after a 202, so a lost response guarantees
    a retry. That retry is a new request with a new received_at, which the
    database key cannot catch — the Redis window does."""
    event = sample_event()
    first = await tracker.post("/v1/events", json={"events": [event]})
    second = await tracker.post("/v1/events", json={"events": [event]})

    assert first.json() == {"accepted": 1, "duplicates": 0, "dropped": 0}
    assert second.json() == {"accepted": 0, "duplicates": 1, "dropped": 0}


async def test_clock_skew_is_recorded_not_trusted(
    tracker, ingest_redis, worker_consumer, owner_conn, seeded_app
):
    """A device with a wrong clock must not land in last month's partition."""
    from tests.conftest_ingest import flush_tracker

    await tracker.post(
        "/v1/events",
        json={"events": [sample_event(occurred_at="2020-01-01T00:00:00Z")]},
    )
    await flush_tracker(tracker)
    await worker_consumer.run_once()

    row = await owner_conn.fetchrow(
        "SELECT received_at, occurred_at, clock_skew_ms FROM events WHERE app_id = $1",
        seeded_app["app_id"],
    )
    assert row is not None
    assert row["clock_skew_ms"] is not None and row["clock_skew_ms"] < 0
    # Clamped to server time so the row is queryable where a reader expects it.
    assert row["received_at"].year >= 2026
    assert row["occurred_at"].year >= 2026


async def test_rate_limited_per_app(tracker, ingest_redis, seeded_app):
    from mmp_core.ratelimit import RateLimit

    state = tracker.tracker_app.state.tracker
    state.ingest_limit = RateLimit(capacity=2, refill_per_second=0.01)

    statuses = [
        (await tracker.post("/v1/events", json={"events": [sample_event()]})).status_code
        for _ in range(5)
    ]
    assert 429 in statuses
    assert statuses[:2] == [202, 202]
