"""The click redirect: routing, latency discipline, and the click record."""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

import pytest

from tests.conftest_ingest import ANDROID_UA, DESKTOP_UA, IOS_UA, flush_tracker


async def _drain(consumer, *, rounds: int = 5) -> None:
    for _ in range(rounds):
        if await consumer.run_once() == 0:
            break


async def test_redirect_is_a_302_to_the_platform_store(tracker, seeded_app):
    response = await tracker.get(
        f"/c/{seeded_app['tracking_code']}",
        headers={"user-agent": ANDROID_UA},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://play.google.com/")


async def test_ios_and_web_route_to_their_own_destinations(tracker, seeded_app):
    ios = await tracker.get(
        f"/c/{seeded_app['tracking_code']}",
        headers={"user-agent": IOS_UA},
        follow_redirects=False,
    )
    assert ios.headers["location"].startswith("https://apps.apple.com/")

    web = await tracker.get(
        f"/c/{seeded_app['tracking_code']}",
        headers={"user-agent": DESKTOP_UA},
        follow_redirects=False,
    )
    assert web.headers["location"].startswith("https://example.com/landing")


async def test_android_click_id_travels_in_the_play_referrer(tracker, seeded_app):
    """The highest-fidelity attribution signal on any platform.

    Play returns this string through the Install Referrer API on first launch,
    which is what makes an Android install deterministically attributable rather
    than inferred. If the click id is not in here, Android attribution silently
    degrades to guessing.
    """
    response = await tracker.get(
        f"/c/{seeded_app['tracking_code']}",
        headers={"user-agent": ANDROID_UA},
        follow_redirects=False,
    )
    query = parse_qs(urlparse(response.headers["location"]).query)
    referrer = unquote(query["referrer"][0])
    assert "utm_content=" in referrer
    click_id = parse_qs(referrer)["utm_content"][0]

    import uuid

    assert uuid.UUID(click_id).version == 7
    assert "deep_link=" in referrer, "deferred deep-link context must survive the install"


async def test_existing_destination_query_is_preserved(tracker, owner_conn, seeded_app):
    """Advertisers put their own parameters on store links. Ours must be added,
    not substituted."""
    await owner_conn.execute(
        "UPDATE tracking_links SET fallback_url = $1 WHERE tracking_code = $2",
        "https://example.com/landing?utm_source=partner&existing=1",
        seeded_app["tracking_code"],
    )
    await tracker.tracker_app.state.tracker.links.resync()

    response = await tracker.get(
        f"/c/{seeded_app['tracking_code']}",
        headers={"user-agent": DESKTOP_UA},
        follow_redirects=False,
    )
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["utm_source"] == ["partner"]
    assert query["existing"] == ["1"]
    assert "mmp_click_id" in query


async def test_every_click_gets_a_distinct_id(tracker, seeded_app):
    ids = set()
    for _ in range(10):
        response = await tracker.get(
            f"/c/{seeded_app['tracking_code']}",
            headers={"user-agent": DESKTOP_UA},
            follow_redirects=False,
        )
        query = parse_qs(urlparse(response.headers["location"]).query)
        ids.add(query["mmp_click_id"][0])
    assert len(ids) == 10


async def test_redirect_is_not_cacheable(tracker, seeded_app):
    """A cached 302 reuses one click_id for every visitor behind that cache,
    collapsing many clicks into one and destroying attribution for all but the
    first. It also must not be a 301, which browsers cache indefinitely."""
    response = await tracker.get(
        f"/c/{seeded_app['tracking_code']}",
        headers={"user-agent": DESKTOP_UA},
        follow_redirects=False,
    )
    assert response.status_code == 302
    cache_control = response.headers["cache-control"]
    assert "no-store" in cache_control
    assert "max-age=0" in cache_control


async def test_unknown_code_is_a_404(tracker):
    response = await tracker.get("/c/doesnotexistatall", follow_redirects=False)
    assert response.status_code == 404


async def test_redirect_does_not_query_postgres_on_the_hot_path(tracker, seeded_app):
    """The cache is the design, not an optimisation.

    A database round trip per redirect would be the largest item in the latency
    budget and would tie every advertiser's campaign availability to ours.
    """
    cache = tracker.tracker_app.state.tracker.links
    await cache.resync()
    before = cache.loads

    for _ in range(20):
        await tracker.get(
            f"/c/{seeded_app['tracking_code']}",
            headers={"user-agent": ANDROID_UA},
            follow_redirects=False,
        )

    assert cache.loads == before, "a cached link must not cause a database lookup"


async def test_cache_miss_falls_through_to_the_database(tracker, owner_conn, seeded_app):
    """A link created moments ago must work before the cache hears about it."""
    import secrets

    from mmp_core.ids import uuid7

    code = f"fresh{secrets.token_hex(6)}"
    await owner_conn.execute(
        """INSERT INTO tracking_links (id, organization_id, app_id, campaign_id, tracking_code,
                                       name, fallback_url, status)
           VALUES ($1, $2, $3, $4, $5, 'Fresh', 'https://example.com/fresh', 'active')""",
        uuid7(),
        seeded_app["organization_id"],
        seeded_app["app_id"],
        seeded_app["campaign_id"],
        code,
    )
    try:
        response = await tracker.get(f"/c/{code}", follow_redirects=False)
        assert response.status_code == 302
        assert "example.com/fresh" in response.headers["location"]
    finally:
        await owner_conn.execute("DELETE FROM tracking_links WHERE tracking_code = $1", code)


async def test_unknown_codes_are_negatively_cached(tracker):
    """Otherwise a flood of bad codes becomes a flood of database lookups."""
    cache = tracker.tracker_app.state.tracker.links
    before = cache.loads
    for _ in range(10):
        await tracker.get("/c/neverexisted", follow_redirects=False)
    assert cache.loads - before == 1, "only the first miss should reach the database"


async def test_disabled_link_stops_redirecting(tracker, owner_conn, seeded_app):
    await owner_conn.execute(
        "UPDATE tracking_links SET status = 'disabled' WHERE tracking_code = $1",
        seeded_app["tracking_code"],
    )
    await tracker.tracker_app.state.tracker.links.resync()

    response = await tracker.get(f"/c/{seeded_app['tracking_code']}", follow_redirects=False)
    assert response.status_code == 404


async def test_disabling_the_app_disables_its_links(tracker, owner_conn, seeded_app):
    """Otherwise turning off an app stops its ingestion but leaves its links
    quietly sending traffic to a store listing nobody is measuring."""
    await owner_conn.execute(
        "UPDATE apps SET status = 'disabled' WHERE id = $1", seeded_app["app_id"]
    )
    await tracker.tracker_app.state.tracker.links.resync()

    response = await tracker.get(f"/c/{seeded_app['tracking_code']}", follow_redirects=False)
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("user_agent", "expect_bot"),
    [
        (ANDROID_UA, False),
        (IOS_UA, False),
        ("curl/8.4.0", True),
        ("facebookexternalhit/1.1", True),
        ("", True),  # httpx always sends its own UA unless explicitly blanked
    ],
)
async def test_bots_are_flagged_but_never_blocked(
    tracker, click_consumer, owner_conn, seeded_app, user_agent, expect_bot
):
    """Blocking a false positive costs a real conversion. Flagging costs a row
    in a report someone can review."""
    headers = {"user-agent": user_agent}
    response = await tracker.get(
        f"/c/{seeded_app['tracking_code']}", headers=headers, follow_redirects=False
    )
    assert response.status_code == 302, "a suspected bot must still be redirected"

    await flush_tracker(tracker)
    await _drain(click_consumer)

    row = await owner_conn.fetchrow(
        "SELECT is_bot FROM clicks WHERE app_id = $1 ORDER BY clicked_at DESC LIMIT 1",
        seeded_app["app_id"],
    )
    assert row is not None
    assert row["is_bot"] is expect_bot


async def test_click_reaches_postgres_with_its_context(
    tracker, click_consumer, owner_conn, seeded_app
):
    response = await tracker.get(
        f"/c/{seeded_app['tracking_code']}?sub1=affiliate7&sub2=creative3&gaid=ABC-123",
        headers={"user-agent": ANDROID_UA, "x-forwarded-for": "203.0.113.9"},
        follow_redirects=False,
    )
    assert response.status_code == 302

    await flush_tracker(tracker)
    await _drain(click_consumer)

    row = await owner_conn.fetchrow("SELECT * FROM clicks WHERE app_id = $1", seeded_app["app_id"])
    assert row is not None
    assert row["campaign_id"] == seeded_app["campaign_id"]
    assert row["tracking_link_id"] == seeded_app["tracking_link_id"]
    assert row["sub1"] == "affiliate7"
    assert row["sub2"] == "creative3"
    assert row["platform"] == 1  # android
    assert row["os_version"] == "14"
    # Both identifiers are hashed. The raw values must never be stored.
    assert row["ip_hash"] is not None and b"203.0.113.9" not in bytes(row["ip_hash"])
    assert row["device_hash"] is not None and b"ABC-123" not in bytes(row["device_hash"])


async def test_click_id_in_the_redirect_matches_the_stored_row(
    tracker, click_consumer, owner_conn, seeded_app
):
    """The whole attribution chain depends on this: the id handed to the store
    must be the id we recorded."""
    response = await tracker.get(
        f"/c/{seeded_app['tracking_code']}",
        headers={"user-agent": DESKTOP_UA},
        follow_redirects=False,
    )
    query = parse_qs(urlparse(response.headers["location"]).query)
    redirected_id = query["mmp_click_id"][0]

    await flush_tracker(tracker)
    await _drain(click_consumer)

    stored = await owner_conn.fetchval(
        "SELECT click_id FROM clicks WHERE app_id = $1", seeded_app["app_id"]
    )
    assert str(stored) == redirected_id


async def test_click_redelivery_is_deduplicated(
    tracker, click_consumer, ingest_redis, owner_conn, seeded_app
):
    from mmp_ingest.stream import CLICKS_GROUP, CLICKS_STREAM

    for _ in range(5):
        await tracker.get(
            f"/c/{seeded_app['tracking_code']}",
            headers={"user-agent": ANDROID_UA},
            follow_redirects=False,
        )
    await flush_tracker(tracker)

    first = await click_consumer._consumer.read(count=100)
    assert len(first) == 5
    await click_consumer._process(first)

    await ingest_redis.xgroup_setid(CLICKS_STREAM, CLICKS_GROUP, id="0")
    second = await click_consumer._consumer.read(count=100)
    await click_consumer._process(second)

    count = await owner_conn.fetchval(
        "SELECT count(*) FROM clicks WHERE app_id = $1", seeded_app["app_id"]
    )
    assert count == 5
