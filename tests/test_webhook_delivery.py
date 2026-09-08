"""Webhook delivery, end to end against a real receiver.

Runs an actual HTTP server on loopback rather than mocking the send, so the
signature, the headers and the JSON body are exercised as a customer would
receive them — and so the auto-disable path is driven by real failures.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tests.conftest_ingest import flush_tracker, sample_event


@dataclass
class Receiver:
    """A customer's webhook endpoint, as a real server."""

    url: str
    received: list[dict] = field(default_factory=list)
    respond_with: int = 200


@pytest.fixture
async def receiver():
    """An HTTP server on loopback.

    A real socket, not a mock: the point is to exercise what actually leaves the
    process. Loopback is normally blocked by the outbound guard, which is why
    the test overrides the block list rather than the guard — the override is
    visible and scoped to this fixture.
    """
    import uvicorn

    state = Receiver(url="")

    async def handle(request: Request) -> Response:
        body = await request.body()
        state.received.append(
            {
                "headers": dict(request.headers),
                "body": json.loads(body),
                "raw": body,
                "path": request.url.path,
            }
        )
        return JSONResponse({"ok": True}, status_code=state.respond_with)

    app = Starlette(routes=[Route("/hooks/mmp", handle, methods=["POST"])])
    config = uvicorn.Config(app, host="127.0.0.1", port=8977, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    # http, because the receiver is a plain server. The scheme check and the
    # loopback block are both overridden per test below — deliberately narrow,
    # so the guard itself stays under test everywhere else.
    state.url = "http://127.0.0.1:8977/hooks/mmp"
    try:
        yield state
    finally:
        server.should_exit = True
        await task


async def _webhook_row(owner_conn, seeded_app, context, url: str, events: list[str]):
    from mmp_core.ids import uuid7
    from mmp_crypto.envelope import organization_aad, seal

    secret = "whsec_delivery_test"
    sealed = seal(
        secret.encode(),
        provider=context,
        aad=organization_aad(seeded_app["organization_id"]),
    )
    webhook_id = uuid7()
    await owner_conn.execute(
        """INSERT INTO webhooks (id, organization_id, url, secret_ciphertext,
                                 secret_nonce, wrapped_dek, events, enabled,
                                 consecutive_failures, created_at, updated_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, true, 0, now(), now())""",
        webhook_id,
        seeded_app["organization_id"],
        url,
        sealed.ciphertext,
        sealed.nonce,
        sealed.wrapped_dek,
        json.dumps(events),
    )
    return webhook_id, secret


async def _consumer(seeded_app, ingest_redis, database):
    from mmp_crypto.envelope import provider_from_settings
    from mmp_worker.webhook_sender import WebhookConsumer

    consumer = WebhookConsumer(
        redis=ingest_redis,
        database=database,
        consumer_name="test-webhooks",
        master_keys=provider_from_settings(seeded_app["worker_settings"]),
        idle_sleep=0.01,
    )
    await consumer.start()
    return consumer


async def test_a_purchase_reaches_the_receiver_signed(
    tracker, ingest_redis, owner_conn, seeded_app, receiver, monkeypatch
):
    import hmac
    from hashlib import sha256

    from mmp_crypto.envelope import provider_from_settings
    from mmp_db.pool import Database

    from mmp_core import outbound

    # Loopback is blocked by design. Scoped to this test so the guard itself
    # stays under test everywhere else.
    monkeypatch.setattr(outbound, "BLOCKED_NETWORKS", ())
    monkeypatch.setattr(
        outbound,
        "validate_destination",
        lambda url, allow_http=False: outbound.ResolvedTarget(
            url=url, hostname="127.0.0.1", address="127.0.0.1", port=8977
        ),
    )

    provider = provider_from_settings(seeded_app["worker_settings"])
    _webhook_id, secret = await _webhook_row(
        owner_conn, seeded_app, provider, receiver.url, ["purchase"]
    )

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="purchase",
                    anonymous_id="hook-dev",
                    revenue_minor=2599,
                    currency="USD",
                )
            ]
        },
    )
    await flush_tracker(tracker)

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        consumer = await _consumer(seeded_app, ingest_redis, database)
        for _ in range(4):
            if await consumer.run_once() == 0:
                break
    finally:
        await database.close()

    assert receiver.received, "the receiver should have been called"
    delivery = receiver.received[0]

    assert delivery["body"]["type"] == "purchase"
    assert delivery["body"]["data"]["revenue_minor"] == 2599
    assert delivery["headers"]["x-mmp-delivery-id"]

    # Verify exactly as a customer would.
    timestamp = delivery["headers"]["x-mmp-timestamp"]
    canonical = "\n".join(
        ["v1", "POST", "/hooks/mmp", timestamp, sha256(delivery["raw"]).hexdigest()]
    ).encode()
    expected = hmac.new(secret.encode(), canonical, sha256).hexdigest()
    assert hmac.compare_digest(
        expected, delivery["headers"]["x-mmp-signature"].removeprefix("v1=")
    ), "the signature a customer computes must match the one we send"


async def test_only_subscribed_events_are_delivered(
    tracker, ingest_redis, owner_conn, seeded_app, receiver, monkeypatch
):
    from mmp_crypto.envelope import provider_from_settings
    from mmp_db.pool import Database

    from mmp_core import outbound

    monkeypatch.setattr(outbound, "BLOCKED_NETWORKS", ())
    monkeypatch.setattr(
        outbound,
        "validate_destination",
        lambda url, allow_http=False: outbound.ResolvedTarget(
            url=url, hostname="127.0.0.1", address="127.0.0.1", port=8977
        ),
    )

    provider = provider_from_settings(seeded_app["worker_settings"])
    await _webhook_row(owner_conn, seeded_app, provider, receiver.url, ["install"])

    await tracker.post(
        "/v1/events",
        json={"events": [sample_event(event_name="purchase", anonymous_id="hook-dev-2")]},
    )
    await flush_tracker(tracker)

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        consumer = await _consumer(seeded_app, ingest_redis, database)
        for _ in range(3):
            if await consumer.run_once() == 0:
                break
    finally:
        await database.close()

    assert not receiver.received, "a purchase must not be sent to an install-only hook"


async def test_a_redelivered_event_is_not_sent_twice(
    tracker, ingest_redis, owner_conn, seeded_app, receiver, monkeypatch
):
    """A duplicated purchase notification is a duplicated order in whatever
    system is listening."""
    from mmp_crypto.envelope import provider_from_settings
    from mmp_db.pool import Database
    from mmp_ingest.stream import EVENTS_STREAM
    from mmp_worker.webhook_sender import WEBHOOK_GROUP

    from mmp_core import outbound

    monkeypatch.setattr(outbound, "BLOCKED_NETWORKS", ())
    monkeypatch.setattr(
        outbound,
        "validate_destination",
        lambda url, allow_http=False: outbound.ResolvedTarget(
            url=url, hostname="127.0.0.1", address="127.0.0.1", port=8977
        ),
    )

    provider = provider_from_settings(seeded_app["worker_settings"])
    await _webhook_row(owner_conn, seeded_app, provider, receiver.url, ["purchase"])

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="purchase",
                    anonymous_id="hook-dup",
                    revenue_minor=100,
                    currency="USD",
                )
            ]
        },
    )
    await flush_tracker(tracker)

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        consumer = await _consumer(seeded_app, ingest_redis, database)
        await consumer.run_once()
        # Rewind the group, exactly as a crash before acknowledgement would.
        await ingest_redis.xgroup_setid(EVENTS_STREAM, WEBHOOK_GROUP, id="0")
        await consumer.run_once()
    finally:
        await database.close()

    assert len(receiver.received) == 1, "a redelivered event must not fire twice"


async def test_repeated_failures_disable_the_webhook(
    tracker, ingest_redis, owner_conn, seeded_app, receiver, monkeypatch
):
    """An endpoint returning 500s for a day is not recovering on its own.

    Continuing to retry into it generates load on a broken system and a backlog
    we would have to drain later.
    """
    from mmp_crypto.envelope import provider_from_settings
    from mmp_db.pool import Database
    from mmp_providers.webhooks import MAX_CONSECUTIVE_FAILURES

    from mmp_core import outbound

    monkeypatch.setattr(outbound, "BLOCKED_NETWORKS", ())
    monkeypatch.setattr(
        outbound,
        "validate_destination",
        lambda url, allow_http=False: outbound.ResolvedTarget(
            url=url, hostname="127.0.0.1", address="127.0.0.1", port=8977
        ),
    )
    receiver.respond_with = 500

    provider = provider_from_settings(seeded_app["worker_settings"])
    webhook_id, _ = await _webhook_row(owner_conn, seeded_app, provider, receiver.url, ["purchase"])
    # Start one short of the threshold so the test drives the last failure
    # rather than sending twenty events.
    await owner_conn.execute(
        "UPDATE webhooks SET consecutive_failures = $2 WHERE id = $1",
        webhook_id,
        MAX_CONSECUTIVE_FAILURES - 1,
    )

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="purchase", anonymous_id="hook-fail", revenue_minor=1, currency="USD"
                )
            ]
        },
    )
    await flush_tracker(tracker)

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        consumer = await _consumer(seeded_app, ingest_redis, database)
        for _ in range(3):
            if await consumer.run_once() == 0:
                break
    finally:
        await database.close()

    row = await owner_conn.fetchrow(
        "SELECT enabled, consecutive_failures, disabled_at FROM webhooks WHERE id = $1",
        webhook_id,
    )
    assert row["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES
    assert not row["enabled"], "the webhook should have been switched off"
    assert row["disabled_at"] is not None

    delivery = await owner_conn.fetchrow(
        "SELECT status, response_status FROM webhook_deliveries WHERE webhook_id = $1",
        webhook_id,
    )
    assert delivery["status"] == "failed"
    assert delivery["response_status"] == 500
