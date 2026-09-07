"""Fixtures for the ingest pipeline: a live tracker, stream and worker."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from mmp_core.ids import uuid7
from mmp_crypto.keys import generate_key
from redis.asyncio import Redis

from tests.conftest_api import TEST_REDIS_URL, build_api_settings, build_settings_for


@pytest_asyncio.fixture
async def ingest_redis() -> AsyncIterator[Redis]:
    redis = Redis.from_url(TEST_REDIS_URL, decode_responses=False)
    try:
        await redis.ping()
    except Exception:
        pytest.skip("redis not reachable")
    await redis.flushdb()
    try:
        yield redis
    finally:
        await redis.flushdb()
        await redis.aclose()


@pytest_asyncio.fixture
async def seeded_app(owner_conn, ingest_redis):
    """An organisation, an app, and a working API key.

    Created directly rather than through the API so that ingest tests do not
    depend on the auth surface — a failure here should point at ingestion.
    """
    # One shared pepper across the fixture, and a DSN per role: the tracker
    # authenticates as mmp_tracker, the worker writes as mmp_worker.
    settings = build_api_settings()
    tracker_settings = build_settings_for("mmp_tracker").model_copy(
        update={"api_key_pepper": settings.api_key_pepper}
    )
    worker_settings = build_settings_for("mmp_worker").model_copy(
        update={"api_key_pepper": settings.api_key_pepper}
    )
    org_id, app_id, key_id = uuid7(), uuid7(), uuid7()
    suffix = secrets.token_hex(4)

    await owner_conn.execute(
        "INSERT INTO organizations (id, name, slug, timezone) VALUES ($1, $2, $3, 'UTC')",
        org_id,
        f"ingest-{suffix}",
        f"ingest-{suffix}",
    )
    await owner_conn.execute(
        """INSERT INTO apps (id, organization_id, name, platform, android_package_name,
                             status, install_window_days, event_window_days,
                             session_timeout_minutes, timezone)
           VALUES ($1, $2, 'Ingest App', 'android', 'com.example.ingest', 'active',
                   7, 30, 30, 'UTC')""",
        app_id,
        org_id,
    )
    generated = generate_key(environment="prod", pepper=settings.api_key_pepper)
    await owner_conn.execute(
        """INSERT INTO api_keys (id, organization_id, app_id, name, kind, key_prefix,
                                 key_hash, pepper_version, environment, status)
           VALUES ($1, $2, $3, 'ingest', 'sdk', $4, $5, 1, 'prod', 'active')""",
        key_id,
        org_id,
        app_id,
        generated.prefix,
        generated.key_hash,
    )

    yield {
        "organization_id": org_id,
        "app_id": app_id,
        "api_key": generated.raw,
        "settings": settings,
        "tracker_settings": tracker_settings,
        "worker_settings": worker_settings,
    }

    await owner_conn.execute("DELETE FROM events WHERE app_id = $1", app_id)
    await owner_conn.execute("DELETE FROM organizations WHERE id = $1", org_id)


@pytest_asyncio.fixture
async def tracker(seeded_app, ingest_redis) -> AsyncIterator[httpx.AsyncClient]:
    from mmp_tracker.app import create_app

    app = create_app(seeded_app["tracker_settings"])
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://tracker") as client,
    ):
        client.headers["authorization"] = f"Bearer {seeded_app['api_key']}"
        # Expose the live app so tests can reach the buffer directly.
        client.tracker_app = app  # type: ignore[attr-defined]
        yield client


@pytest_asyncio.fixture
async def worker_consumer(ingest_redis, seeded_app):
    """A consumer bound to the test stream, driven one batch at a time."""
    from mmp_db.pool import Database
    from mmp_worker.consumers import EventConsumer

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    consumer = EventConsumer(
        redis=ingest_redis, database=database, consumer_name="test-consumer", idle_sleep=0.01
    )
    await consumer.start()
    try:
        yield consumer
    finally:
        await database.close()


async def flush_tracker(client: httpx.AsyncClient) -> None:
    """Force the shipping buffer out to Redis without waiting for its timer."""
    state = client.tracker_app.state.tracker  # type: ignore[attr-defined]
    await state.buffer._flush_once()


def sample_event(**overrides) -> dict:
    event = {
        "event_id": str(uuid7()),
        "event_name": "install",
        "anonymous_id": "device-abc",
        "platform": "android",
        "app_version": "1.0.0",
    }
    event.update(overrides)
    return event
