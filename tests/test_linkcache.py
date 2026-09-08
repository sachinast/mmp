"""Cache freshness: how quickly a change reaches the tracker processes."""

from __future__ import annotations

import asyncio
import secrets

import pytest
from mmp_core.ids import uuid7

from tests.conftest_api import build_settings_for


@pytest.fixture
def link_row(seeded_app):
    return {
        "organization_id": seeded_app["organization_id"],
        "app_id": seeded_app["app_id"],
        "campaign_id": seeded_app["campaign_id"],
    }


async def _make_cache(seeded_app):
    from mmp_db.pool import Database
    from mmp_tracker.linkcache import LinkCache

    settings = build_settings_for("mmp_tracker").model_copy(
        update={"api_key_pepper": seeded_app["settings"].api_key_pepper}
    )
    database = await Database.connect(settings, role="mmp_tracker")
    cache = LinkCache(database)
    await cache.start()
    return cache, database


async def test_cache_loads_active_links_at_startup(seeded_app):
    cache, database = await _make_cache(seeded_app)
    try:
        assert cache.get(seeded_app["tracking_code"]) is not None
        assert cache.size >= 1
    finally:
        await cache.stop()
        await database.close()


async def test_notify_propagates_a_change_without_a_resync(seeded_app, owner_conn):
    """The reason LISTEN/NOTIFY is here at all.

    Without it, disabling a link would keep redirecting for up to a full resync
    interval — five minutes during which an advertiser believes they have turned
    something off and it is still running.
    """
    cache, database = await _make_cache(seeded_app)
    try:
        code = seeded_app["tracking_code"]
        assert cache.get(code) is not None
        resyncs_before = cache.resyncs

        await owner_conn.execute(
            "UPDATE tracking_links SET status = 'disabled' WHERE tracking_code = $1", code
        )
        await owner_conn.execute("SELECT pg_notify('tracking_links_changed', $1)", code)

        for _ in range(50):
            await asyncio.sleep(0.02)
            if cache.get(code) is None:
                break

        assert cache.get(code) is None, "the disabled link should have been evicted"
        assert cache.notifications >= 1
        assert cache.resyncs == resyncs_before, "no full resync should have been needed"
    finally:
        await cache.stop()
        await database.close()


async def test_notify_makes_a_new_link_live(seeded_app, owner_conn, link_row):
    cache, database = await _make_cache(seeded_app)
    code = f"live{secrets.token_hex(6)}"
    try:
        assert cache.get(code) is None
        await owner_conn.execute(
            """INSERT INTO tracking_links (id, organization_id, app_id, campaign_id,
                                           tracking_code, name, fallback_url, status)
               VALUES ($1, $2, $3, $4, $5, 'New', 'https://example.com/new', 'active')""",
            uuid7(),
            link_row["organization_id"],
            link_row["app_id"],
            link_row["campaign_id"],
            code,
        )
        await owner_conn.execute("SELECT pg_notify('tracking_links_changed', $1)", code)

        for _ in range(50):
            await asyncio.sleep(0.02)
            if cache.get(code) is not None:
                break
        assert cache.get(code) is not None
    finally:
        await owner_conn.execute("DELETE FROM tracking_links WHERE tracking_code = $1", code)
        await cache.stop()
        await database.close()


async def test_resync_recovers_a_missed_notification(seeded_app, owner_conn):
    """Notifications are fire-and-forget.

    A process that was disconnected when one was sent never learns about it, so
    the periodic full resync is what makes this design safe rather than merely
    fast. Simulated here by changing the row without notifying.
    """
    cache, database = await _make_cache(seeded_app)
    try:
        code = seeded_app["tracking_code"]
        assert cache.get(code) is not None

        # No pg_notify: exactly what a dropped notification looks like.
        await owner_conn.execute(
            "UPDATE tracking_links SET status = 'disabled' WHERE tracking_code = $1", code
        )
        assert cache.get(code) is not None, "the cache should still be stale at this point"

        await cache.resync()
        assert cache.get(code) is None
    finally:
        await cache.stop()
        await database.close()


async def test_resync_swaps_atomically(seeded_app):
    """A redirect must never observe a half-loaded cache and 404 a live link."""
    cache, database = await _make_cache(seeded_app)
    try:
        code = seeded_app["tracking_code"]
        seen_missing = False

        async def hammer() -> None:
            nonlocal seen_missing
            for _ in range(200):
                if cache.get(code) is None:
                    seen_missing = True
                await asyncio.sleep(0)

        await asyncio.gather(hammer(), cache.resync(), cache.resync(), cache.resync())
        assert not seen_missing, "a resync must not expose an empty or partial cache"
    finally:
        await cache.stop()
        await database.close()


async def test_cache_survives_a_lost_listener(seeded_app):
    """Losing the LISTEN connection must degrade to the resync interval, not
    take the tracker down."""
    cache, database = await _make_cache(seeded_app)
    try:
        if cache._listener is not None:
            await cache._listener.close()
        # Reads keep working from the in-process dict.
        assert cache.get(seeded_app["tracking_code"]) is not None
        assert await cache.resync() >= 1
    finally:
        await cache.stop()
        await database.close()
