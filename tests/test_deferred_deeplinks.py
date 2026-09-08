"""Deferred deep links, end to end through the real redirect and tracker.

The destination has to survive three hops: the click that carried it, the
attribution that inherited it, and the handshake that gives it back to the app.
Each hop is somewhere it could be silently dropped, and dropping it degrades
quietly to a normal install — nothing errors, the person just lands on the wrong
screen. So each hop is asserted separately.
"""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

from mmp_core.ids import uuid7

from tests.conftest_ingest import ANDROID_UA, flush_tracker, sample_event


async def _drain(*consumers, rounds: int = 5) -> None:
    for _ in range(rounds):
        if sum([await c.run_once() for c in consumers]) == 0:
            break


async def _click(tracker, seeded_app, query: str = ""):
    response = await tracker.get(
        f"/c/{seeded_app['tracking_code']}{query}",
        headers={"user-agent": ANDROID_UA},
        follow_redirects=False,
    )
    location = response.headers["location"]
    referrer = unquote(parse_qs(urlparse(location).query)["referrer"][0])
    return response, location, parse_qs(referrer)


async def test_a_raw_path_reaches_the_store_referrer(tracker, seeded_app):
    _, _, referrer = await _click(tracker, seeded_app, "?dl=/product/123")
    assert referrer["deep_link"] == ["/product/123"]


async def test_a_hostile_path_is_dropped_and_the_click_still_redirects(tracker, seeded_app):
    """Degrade, never fail. The person tapping the link is not the attacker and
    should still reach the store; they simply land on the home screen."""
    response, location, referrer = await _click(
        tracker, seeded_app, "?dl=https://evil.example/steal"
    )
    assert response.status_code == 302
    assert "evil.example" not in location
    assert "evil.example" not in str(referrer)


async def test_a_registered_code_resolves_to_its_destination(
    tracker, owner_conn, seeded_app, tracker_links
):
    await owner_conn.execute(
        """INSERT INTO deep_links (id, organization_id, app_id, code, destination,
                                   fallback_url, created_at, updated_at)
           VALUES ($1, $2, $3, 'summer', '/campaigns/summer', 'https://example.com/summer',
                   now(), now())""",
        uuid7(),
        seeded_app["organization_id"],
        seeded_app["app_id"],
    )
    await tracker_links.resync()

    _, _, referrer = await _click(tracker, seeded_app, "?dl_code=summer")
    assert referrer["deep_link"] == ["/campaigns/summer"]


async def test_an_unregistered_code_is_not_treated_as_a_path(tracker, seeded_app, tracker_links):
    """The important negative. If an unknown code fell through to being used as
    a destination, anyone could name any destination just by inventing a code."""
    await tracker_links.resync()

    baseline = (await _click(tracker, seeded_app))[2].get("deep_link")
    _, _, referrer = await _click(tracker, seeded_app, "?dl_code=/evil/path")

    assert "/evil/path" not in str(referrer), "an unknown code must never become a destination"
    assert referrer.get("deep_link") == baseline, (
        "an unresolvable code falls back to the link's own configured destination, "
        "exactly as if no code had been supplied"
    )


async def test_a_registered_code_beats_a_raw_path(tracker, owner_conn, seeded_app, tracker_links):
    """When the advertiser's own registry and the URL disagree, believe the
    advertiser — the URL is whoever wrote the link."""
    await owner_conn.execute(
        """INSERT INTO deep_links (id, organization_id, app_id, code, destination,
                                   fallback_url, created_at, updated_at)
           VALUES ($1, $2, $3, 'official', '/trusted', 'https://example.com/t', now(), now())""",
        uuid7(),
        seeded_app["organization_id"],
        seeded_app["app_id"],
    )
    await tracker_links.resync()

    _, _, referrer = await _click(tracker, seeded_app, "?dl_code=official&dl=/attacker-chosen")
    assert referrer["deep_link"] == ["/trusted"]


async def test_the_destination_survives_to_the_attribution_and_back_to_the_app(
    tracker, click_consumer, attribution_consumer, owner_conn, seeded_app
):
    """The whole point, in one test: click with a destination, install, and the
    app asks what the person was after."""
    _, _, referrer = await _click(tracker, seeded_app, "?dl=/product/999")
    play_referrer = f"utm_content={referrer['utm_content'][0]}&deep_link=%2Fproduct%2F999"

    await flush_tracker(tracker)
    await _drain(click_consumer)

    stored = await owner_conn.fetchval(
        "SELECT deep_link FROM clicks WHERE click_id = $1", referrer["utm_content"][0]
    )
    assert stored == "/product/999", "hop 1: the click must record the destination"

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="install",
                    anonymous_id="deferred-device",
                    properties={"install_referrer": play_referrer},
                )
            ]
        },
    )
    await flush_tracker(tracker)
    await _drain(attribution_consumer)

    inherited = await owner_conn.fetchval(
        "SELECT deep_link FROM attributions WHERE app_id = $1 AND anonymous_id = 'deferred-device'",
        seeded_app["app_id"],
    )
    assert inherited == "/product/999", "hop 2: the attribution must inherit it"

    response = await tracker.post("/v1/deeplink/resolve", json={"anonymous_id": "deferred-device"})
    assert response.status_code == 200
    assert response.json() == {"destination": "/product/999", "matched": True}


async def test_an_unknown_device_gets_the_same_answer_as_a_known_one_with_no_link(
    tracker, seeded_app
):
    """The endpoint must not become an oracle for whether a device installed
    this app. Both cases produce one indistinguishable response."""
    response = await tracker.post(
        "/v1/deeplink/resolve", json={"anonymous_id": "never-seen-before"}
    )
    assert response.status_code == 200
    assert response.json() == {"destination": None, "matched": False}


async def test_resolution_requires_a_key(tracker, seeded_app):
    tracker.headers.pop("authorization", None)
    try:
        response = await tracker.post("/v1/deeplink/resolve", json={"anonymous_id": "whoever"})
        assert response.status_code == 401
    finally:
        tracker.headers["authorization"] = f"Bearer {seeded_app['api_key']}"
