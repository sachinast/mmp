"""Postback delivery end to end, against a real ad-network endpoint.

A real HTTP server rather than a mocked send, because the bugs this path has
produced were all in the seam between components: a status-code list read as a
string, a URL built from a template, a claim that did not prevent a second send.
None of those are visible from either side alone.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from urllib.parse import parse_qs

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from tests.conftest_ingest import flush_tracker, sample_event


@dataclass
class Network:
    """An ad network's conversion endpoint."""

    url: str = ""
    received: list[dict] = field(default_factory=list)
    respond_with: int = 200


@pytest.fixture
async def network():
    import uvicorn

    state = Network()

    async def handle(request: Request) -> Response:
        state.received.append(
            {"query": dict(parse_qs(request.url.query)), "path": request.url.path}
        )
        return PlainTextResponse("ok", status_code=state.respond_with)

    app = Starlette(routes=[Route("/conv", handle, methods=["GET", "POST"])])
    config = uvicorn.Config(app, host="127.0.0.1", port=8978, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    state.url = "http://127.0.0.1:8978/conv"
    try:
        yield state
    finally:
        server.should_exit = True
        await task


@pytest.fixture
def allow_loopback(monkeypatch):
    """Let the outbound client reach the test server.

    Scoped to these tests, and it replaces only the destination check — the
    request itself still goes through the real client. The guard stays under
    test everywhere else.
    """
    from mmp_core import outbound

    monkeypatch.setattr(
        outbound,
        "validate_destination",
        lambda url, allow_http=False: outbound.ResolvedTarget(
            url=url, hostname="127.0.0.1", address="127.0.0.1", port=8978
        ),
    )


async def _rule(owner_conn, seeded_app, network, **overrides):
    from mmp_core.ids import uuid7

    settings = {
        "url_template": network.url + "?click={{click_id}}&rev={{revenue}}&cur={{currency}}",
        "success_status_codes": [200],
        "requires_attribution": False,
        "trigger_event": "purchase",
    }
    settings.update(overrides)

    rule_id = uuid7()
    await owner_conn.execute(
        """INSERT INTO postback_rules (id, organization_id, app_id, name, trigger_event,
                                       method, url_template, success_status_codes,
                                       requires_attribution, is_sandbox, enabled,
                                       created_at, updated_at)
           VALUES ($1, $2, $3, 'Network', $4, 'GET', $5, $6::jsonb, $7, false, true,
                   now(), now())""",
        rule_id,
        seeded_app["organization_id"],
        seeded_app["app_id"],
        settings["trigger_event"],
        settings["url_template"],
        json.dumps(settings["success_status_codes"]),
        settings["requires_attribution"],
    )
    return rule_id


async def _run(seeded_app, ingest_redis, rounds: int = 4):
    from mmp_db.pool import Database
    from mmp_worker.postbacks import PostbackConsumer

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        consumer = PostbackConsumer(
            redis=ingest_redis,
            database=database,
            consumer_name="test-postbacks",
            idle_sleep=0.01,
        )
        await consumer.start()
        for _ in range(rounds):
            if await consumer.run_once() == 0:
                break
        return consumer
    finally:
        await database.close()


async def test_a_conversion_reaches_the_network(
    tracker, ingest_redis, owner_conn, seeded_app, network, allow_loopback
):
    rule_id = await _rule(owner_conn, seeded_app, network)

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="purchase", anonymous_id="pb-dev", revenue_minor=2599, currency="USD"
                )
            ]
        },
    )
    await flush_tracker(tracker)
    await _run(seeded_app, ingest_redis)

    assert network.received, "the network endpoint should have been called"
    query = network.received[0]["query"]
    # Major units, because that is what every network's macro expects.
    assert query["rev"] == ["25.99"]
    assert query["cur"] == ["USD"]

    delivery = await owner_conn.fetchrow(
        "SELECT status, response_status, request_url FROM postback_deliveries "
        "WHERE postback_rule_id = $1",
        rule_id,
    )
    assert delivery["status"] == "delivered", (
        "a 200 against a success list of [200] must record as delivered — this is "
        "the check that read the status list as a string and never matched"
    )
    assert delivery["response_status"] == 200
    assert delivery["request_url"], "the URL is stored so a retry can re-send it"


async def test_a_redelivered_conversion_is_not_sent_twice(
    tracker, ingest_redis, owner_conn, seeded_app, network, allow_loopback
):
    """On cost-per-action, a duplicate means paying twice for one action."""
    from mmp_ingest.stream import EVENTS_STREAM
    from mmp_worker.postbacks import POSTBACK_GROUP

    await _rule(owner_conn, seeded_app, network)
    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="purchase", anonymous_id="pb-dup", revenue_minor=100, currency="USD"
                )
            ]
        },
    )
    await flush_tracker(tracker)

    await _run(seeded_app, ingest_redis, rounds=1)
    await ingest_redis.xgroup_setid(EVENTS_STREAM, POSTBACK_GROUP, id="0")
    await _run(seeded_app, ingest_redis, rounds=1)

    assert len(network.received) == 1


async def test_unattributed_conversions_are_skipped_when_required(
    tracker, ingest_redis, owner_conn, seeded_app, network, allow_loopback
):
    """Reporting an unattributed conversion would credit a network for
    something it did not cause."""
    await _rule(owner_conn, seeded_app, network, requires_attribution=True)

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="purchase",
                    anonymous_id="pb-unattributed",
                    revenue_minor=500,
                    currency="USD",
                )
            ]
        },
    )
    await flush_tracker(tracker)
    consumer = await _run(seeded_app, ingest_redis)

    assert not network.received
    assert consumer.metrics.skipped_unattributed >= 1


async def test_a_failing_network_is_recorded_for_retry(
    tracker, ingest_redis, owner_conn, seeded_app, network, allow_loopback
):
    network.respond_with = 503
    rule_id = await _rule(owner_conn, seeded_app, network)

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="purchase", anonymous_id="pb-fail", revenue_minor=100, currency="USD"
                )
            ]
        },
    )
    await flush_tracker(tracker)
    await _run(seeded_app, ingest_redis)

    delivery = await owner_conn.fetchrow(
        "SELECT status, response_status, next_retry_at FROM postback_deliveries "
        "WHERE postback_rule_id = $1",
        rule_id,
    )
    assert delivery["status"] == "failed"
    assert delivery["response_status"] == 503
    assert delivery["next_retry_at"] is not None, "a 5xx must be scheduled for retry"


async def test_a_refusal_is_not_scheduled_for_retry(
    tracker, ingest_redis, owner_conn, seeded_app, network, allow_loopback
):
    """A 4xx means the network understood us and refused; an identical request
    will be refused identically."""
    network.respond_with = 400
    rule_id = await _rule(owner_conn, seeded_app, network)

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="purchase",
                    anonymous_id="pb-refused",
                    revenue_minor=100,
                    currency="USD",
                )
            ]
        },
    )
    await flush_tracker(tracker)
    await _run(seeded_app, ingest_redis)

    delivery = await owner_conn.fetchrow(
        "SELECT status, next_retry_at FROM postback_deliveries WHERE postback_rule_id = $1",
        rule_id,
    )
    assert delivery["status"] == "abandoned"
    assert delivery["next_retry_at"] is None


async def test_a_due_retry_is_re_sent(
    tracker, ingest_redis, owner_conn, seeded_app, network, allow_loopback
):
    """The queue message was acknowledged when the first attempt was recorded,
    so the retry has only the stored URL to work from."""
    from mmp_db.pool import Database
    from mmp_worker.postbacks import retry_due

    network.respond_with = 503
    rule_id = await _rule(owner_conn, seeded_app, network)

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="purchase",
                    anonymous_id="pb-retry",
                    revenue_minor=100,
                    currency="USD",
                )
            ]
        },
    )
    await flush_tracker(tracker)
    await _run(seeded_app, ingest_redis)
    assert len(network.received) == 1

    # The network recovers, and the backoff elapses.
    network.respond_with = 200
    await owner_conn.execute(
        "UPDATE postback_deliveries SET next_retry_at = now() - interval '1 minute' "
        "WHERE postback_rule_id = $1",
        rule_id,
    )

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        assert await retry_due(database) == 1
    finally:
        await database.close()

    assert len(network.received) == 2, "the retry must actually re-send"
    delivery = await owner_conn.fetchrow(
        "SELECT status, attempt_count FROM postback_deliveries WHERE postback_rule_id = $1",
        rule_id,
    )
    assert delivery["status"] == "delivered"
    assert delivery["attempt_count"] == 2
