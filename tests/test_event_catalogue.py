"""The event catalogue: standard names, custom definitions, discovery, blocking."""

from __future__ import annotations

import datetime as dt
import uuid

from mmp_ingest.catalogue import STANDARD_EVENTS, blocked_events_cache_key, standard_event
from mmp_ingest.schema import SYSTEM_EVENTS, canonical_event_name
from redis.asyncio import Redis

from tests.conftest_api import TEST_REDIS_URL, register_account


def test_every_system_event_is_in_the_catalogue():
    """The names the platform gives semantics to must be the names it documents."""
    names = {event.name for event in STANDARD_EVENTS}
    assert names >= SYSTEM_EVENTS


def test_standard_names_are_unique_on_their_folded_form():
    folded = [canonical_event_name(event.name) for event in STANDARD_EVENTS]
    assert len(folded) == len(set(folded))


def test_a_standard_event_is_found_however_it_is_spelled():
    assert standard_event("Add To Cart") is not None
    assert standard_event("add-to-cart").name == "add_to_cart"
    assert standard_event("mining_started") is None


async def _app(account):
    response = await account.post(
        "/v1/apps",
        json={"name": "Catalogue App", "platform": "android", "android_package_name": "com.ex.cat"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_catalogue_lists_arriving_names_before_unused_standard_ones(api_client, owner_conn):
    account = await register_account(api_client)
    app = await _app(account)
    now = dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
    await owner_conn.execute(
        "INSERT INTO rollup_events_hourly (organization_id, app_id, bucket_hour, event_name,"
        " platform, event_count, unique_devices, revenue_minor) VALUES"
        " ($1, $2, $3, 'mining_started', 1, 40, 7, 0),"
        " ($1, $2, $3, 'purchase', 1, 3, 3, 1500)",
        uuid.UUID(account.organization["id"]),
        uuid.UUID(app["id"]),
        now,
    )

    response = await account.client.get(f"/v1/apps/{app['id']}/events")
    assert response.status_code == 200, response.text
    rows = response.json()
    by_name = {row["name"]: row for row in rows}

    # Arriving names come first, busiest first.
    assert [row["name"] for row in rows[:2]] == ["mining_started", "purchase"]
    assert by_name["mining_started"]["kind"] == "discovered"
    assert by_name["mining_started"]["defined"] is False
    assert by_name["mining_started"]["count_30d"] == 40
    assert by_name["purchase"]["kind"] == "standard"
    assert by_name["purchase"]["revenue"] is True
    assert by_name["purchase"]["revenue_minor_30d"] == 1500
    assert len(by_name["purchase"]["trend"]) == 14
    assert sum(by_name["purchase"]["trend"]) == 3
    # The rest of the standard vocabulary follows, unused.
    assert by_name["add_to_cart"]["count_30d"] == 0
    assert by_name["install"]["sdk_owned"] is True


async def test_defining_a_custom_event_and_blocking_it(api_client):
    account = await register_account(api_client)
    app = await _app(account)
    redis = Redis.from_url(TEST_REDIS_URL)
    try:
        created = await account.post(
            f"/v1/apps/{app['id']}/events",
            json={"name": "withdrawal_requested", "description": "User asked to cash out"},
        )
        assert created.status_code == 201, created.text
        definition = created.json()
        assert definition["kind"] == "custom"
        assert definition["display_name"] == "withdrawal_requested"

        duplicate = await account.post(
            f"/v1/apps/{app['id']}/events", json={"name": "withdrawal_requested"}
        )
        assert duplicate.status_code == 409

        blocked = await account.patch(
            f"/v1/apps/{app['id']}/events/{definition['id']}", json={"status": "blocked"}
        )
        assert blocked.status_code == 200, blocked.text
        assert blocked.json()["status"] == "blocked"
        assert blocked.json()["blocked_at"] is not None

        # Published for the tracker.
        published = await redis.get(blocked_events_cache_key(app["id"]))
        assert published is not None
        assert b"withdrawal_requested" in published

        listed = await account.client.get(f"/v1/apps/{app['id']}/events")
        row = next(r for r in listed.json() if r["name"] == "withdrawal_requested")
        assert row["status"] == "blocked"
        assert row["defined"] is True

        unblocked = await account.patch(
            f"/v1/apps/{app['id']}/events/{definition['id']}", json={"status": "active"}
        )
        assert unblocked.json()["status"] == "active"
        assert unblocked.json()["blocked_at"] is None
        assert await redis.get(blocked_events_cache_key(app["id"])) == b"[]"
    finally:
        await redis.aclose()


async def test_a_standard_event_keeps_the_catalogue_s_revenue_flag(api_client):
    """`purchase` cannot be redefined as non-revenue: reports and the worker
    would then disagree about what it is."""
    account = await register_account(api_client)
    app = await _app(account)
    created = await account.post(
        f"/v1/apps/{app['id']}/events",
        json={"name": "Purchase", "revenue": False, "category": "gaming"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["kind"] == "standard"
    assert created.json()["revenue"] is True
    assert created.json()["category"] == "commerce"
    # Stored as sent, so it matches what the SDK sends.
    assert created.json()["name"] == "Purchase"


async def test_the_standard_catalogue_and_a_missing_definition(api_client):
    account = await register_account(api_client)
    app = await _app(account)
    response = await account.client.get("/v1/events/catalogue")
    assert response.status_code == 200
    assert any(event["name"] == "purchase" for event in response.json())
    # Deleting what does not exist is a 404, not a 500.
    missing = await account.delete(f"/v1/apps/{app['id']}/events/{uuid.uuid4()}")
    assert missing.status_code == 404


async def test_another_tenant_cannot_see_or_block_my_events(api_client):
    mine = await register_account(api_client)
    app = await _app(mine)
    created = await mine.post(f"/v1/apps/{app['id']}/events", json={"name": "secret_event"})
    assert created.status_code == 201

    theirs = await register_account(api_client)
    assert (await theirs.client.get(f"/v1/apps/{app['id']}/events")).status_code == 404
    blocked = await theirs.patch(
        f"/v1/apps/{app['id']}/events/{created.json()['id']}", json={"status": "blocked"}
    )
    assert blocked.status_code == 404
