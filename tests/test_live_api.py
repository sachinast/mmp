"""The live feed: recent activity for one app, for verifying an integration."""

from __future__ import annotations

import datetime as dt
import json
import uuid

import httpx
from mmp_core.ids import uuid7
from mmp_ingest.live import record_rejection

from tests.conftest_api import register_account

NOW = dt.datetime.now(dt.UTC)


async def _app(account, name="Live App", package="com.example.live"):
    response = await account.post(
        "/v1/apps", json={"name": name, "platform": "android", "android_package_name": package}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _seed(owner_conn, org_id: str, app_id: str, *, at: dt.datetime = NOW) -> dict[str, str]:
    """One of everything the feed shows, at ``at``."""
    org, app = uuid.UUID(org_id), uuid.UUID(app_id)
    campaign_id, link_id, rule_id = uuid7(), uuid7(), uuid7()
    click_id, event_id, attribution_id, delivery_id = uuid7(), uuid7(), uuid7(), uuid7()

    await owner_conn.execute(
        "INSERT INTO campaigns (id, organization_id, app_id, name, status, created_at, updated_at)"
        " VALUES ($1, $2, $3, $4, 'active', now(), now())",
        campaign_id,
        org,
        app,
        # Seeded more than once per app by the window tests, and names are unique
        # per app.
        "Live Campaign" if at == NOW else f"Live Campaign {campaign_id}",
    )
    await owner_conn.execute(
        "INSERT INTO tracking_links (id, organization_id, app_id, campaign_id, name, tracking_code,"
        " fallback_url, status, created_at, updated_at)"
        " VALUES ($1, $2, $3, $4, 'live-link', $5, 'https://e.example', 'active', now(), now())",
        link_id,
        org,
        app,
        campaign_id,
        f"live{uuid.uuid4().hex[:12]}",
    )
    await owner_conn.execute(
        "INSERT INTO clicks (click_id, clicked_at, organization_id, app_id, campaign_id,"
        " tracking_link_id, platform, country, device_hash, ip_hash, is_bot)"
        " VALUES ($1, $2, $3, $4, $5, $6, 1, 'IN', $7, $8, false)",
        click_id,
        at,
        org,
        app,
        campaign_id,
        link_id,
        b"DEVICEHASH-MARKER",
        b"IPHASH-MARKER",
    )
    await owner_conn.execute(
        "INSERT INTO events (event_id, received_at, occurred_at, organization_id, app_id,"
        " event_name, anonymous_id, platform, ip_hash, revenue_minor, currency, properties)"
        " VALUES ($1, $2, $2, $3, $4, 'purchase', 'live-device', 1, $5, 4999, 'USD', $6::jsonb)",
        event_id,
        at,
        org,
        app,
        b"IPHASH-MARKER",
        json.dumps({"sku": "SHOE-42"}),
    )
    await owner_conn.execute(
        "INSERT INTO attributions (id, organization_id, app_id, install_key, anonymous_id, method,"
        " campaign_id, tracking_link_id, click_id, installed_at, attributed_at, window_days,"
        " expires_at, created_at, updated_at)"
        " VALUES ($1, $2, $3, $4, 'live-device', 'referrer', $5, $6, $7, $8, $8, 7,"
        " $9, now(), now())",
        attribution_id,
        org,
        app,
        f"k-{attribution_id}",
        campaign_id,
        link_id,
        click_id,
        at,
        at + dt.timedelta(days=30),
    )
    await owner_conn.execute(
        "INSERT INTO postback_rules (id, organization_id, app_id, name, trigger_event, method,"
        " url_template, success_status_codes, requires_attribution, is_sandbox, enabled,"
        " created_at, updated_at)"
        " VALUES ($1, $2, $3, 'Network A', 'purchase', 'GET', 'https://net.example/pb',"
        " '[200]'::jsonb, false, false, true, now(), now())",
        rule_id,
        org,
        app,
    )
    await owner_conn.execute(
        "INSERT INTO postback_deliveries (id, organization_id, postback_rule_id, event_id, status,"
        " attempt_count, request_url, response_status, created_at)"
        " VALUES ($1, $2, $3, $4, 'delivered', 1,"
        " 'https://net.example/pb?click=1&token=PARTNER-SECRET-TOKEN', 200, $5)",
        delivery_id,
        org,
        rule_id,
        event_id,
        at,
    )
    return {
        "click": str(click_id),
        "event": str(event_id),
        "install": str(attribution_id),
        "postback": str(delivery_id),
    }


async def _context_redis(api_client):
    return api_client._transport.app.state.context.redis  # type: ignore[attr-defined]


async def test_the_feed_shows_every_kind_newest_first(account, api_client, owner_conn):
    app = await _app(account)
    ids = await _seed(owner_conn, account.organization["id"], app["id"])
    await record_rejection(
        await _context_redis(api_client),
        app["id"],
        status=422,
        reason="invalid_event",
        detail="event 1: currency is required when revenue_minor is set",
        events_in_batch=2,
    )

    response = await account.client.get("/v1/live", params={"app_id": app["id"]})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()

    kinds = {item["kind"]: item for item in body["items"]}
    assert set(kinds) == {"click", "event", "install", "postback", "rejected"}
    for kind in ("click", "event", "install", "postback"):
        assert kinds[kind]["id"] == ids[kind]

    assert kinds["event"]["title"] == "purchase"
    assert kinds["event"]["details"]["properties"] == {"sku": "SHOE-42"}
    assert kinds["install"]["title"] == "referrer"
    assert kinds["install"]["campaign"] == "Live Campaign"
    assert kinds["rejected"]["details"]["events_in_batch"] == 2

    times = [item["at"] for item in body["items"]]
    assert times == sorted(times, reverse=True), "newest first"
    assert body["server_time"]


async def test_device_and_ip_hashes_never_appear(account, owner_conn):
    """Stable pseudonyms for a person. Nobody verifying an integration needs one."""
    app = await _app(account, package="com.example.hashes")
    await _seed(owner_conn, account.organization["id"], app["id"])

    response = await account.client.get("/v1/live", params={"app_id": app["id"]})
    text = response.text
    for marker in ("DEVICEHASH-MARKER", "IPHASH-MARKER", "ip_hash", "device_hash"):
        assert marker not in text, f"{marker} leaked into the live feed"
    # Not even in an encoded form.
    assert b"IPHASH-MARKER".hex() not in text


async def test_a_postback_url_is_reduced_to_its_host(account, owner_conn):
    """Advertisers put partner tokens in postback query strings."""
    app = await _app(account, package="com.example.pburl")
    await _seed(owner_conn, account.organization["id"], app["id"])

    body = (await account.client.get("/v1/live", params={"app_id": app["id"]})).json()
    postback = next(item for item in body["items"] if item["kind"] == "postback")
    assert postback["details"]["destination_host"] == "net.example"
    assert "PARTNER-SECRET-TOKEN" not in json.dumps(body)


async def test_the_window_is_bounded_whatever_since_asks_for(account, owner_conn):
    """Polled every couple of seconds; it must never become an unbounded scan."""
    app = await _app(account, package="com.example.window")
    old = await _seed(
        owner_conn, account.organization["id"], app["id"], at=NOW - dt.timedelta(minutes=40)
    )
    recent = await _seed(owner_conn, account.organization["id"], app["id"])

    body = (
        await account.client.get(
            "/v1/live",
            params={"app_id": app["id"], "since": (NOW - dt.timedelta(days=30)).isoformat()},
        )
    ).json()
    returned = {item["id"] for item in body["items"]}
    assert recent["event"] in returned
    assert old["event"] not in returned, "anything older than the window is out of reach"
    assert body["window_seconds"] == 900


async def test_since_narrows_to_what_is_new(account, owner_conn):
    app = await _app(account, package="com.example.since")
    first = await _seed(
        owner_conn, account.organization["id"], app["id"], at=NOW - dt.timedelta(minutes=5)
    )
    second = await _seed(owner_conn, account.organization["id"], app["id"])

    body = (
        await account.client.get(
            "/v1/live",
            params={"app_id": app["id"], "since": (NOW - dt.timedelta(minutes=1)).isoformat()},
        )
    ).json()
    returned = {item["id"] for item in body["items"]}
    assert second["event"] in returned
    assert first["event"] not in returned


async def test_another_tenants_app_is_not_found_and_its_rejections_are_not_read(
    api_client, account
):
    """Rows from Postgres are scoped by RLS. Rejections come from Redis, which has
    no idea what a tenant is — the explicit ownership check is the only thing
    stopping one organisation reading another's."""
    victim_app = await _app(account, package="com.example.victim")
    await record_rejection(
        await _context_redis(api_client),
        victim_app["id"],
        status=422,
        reason="invalid_event",
        detail="VICTIM-ONLY-DETAIL",
    )

    intruder = await register_account(api_client, name="Intruder")
    response = await intruder.client.get("/v1/live", params={"app_id": victim_app["id"]})
    assert response.status_code == 404
    assert "VICTIM-ONLY-DETAIL" not in response.text


async def test_a_malformed_app_id_is_simply_not_found(account):
    response = await account.client.get("/v1/live", params={"app_id": "not-a-uuid"})
    assert response.status_code == 404


async def test_large_properties_are_summarised_by_their_keys(account, owner_conn):
    app = await _app(account, package="com.example.bigprops")
    ids = await _seed(owner_conn, account.organization["id"], app["id"])
    big = {f"key_{index}": "x" * 200 for index in range(40)}
    await owner_conn.execute(
        "UPDATE events SET properties = $1::jsonb WHERE event_id = $2",
        json.dumps(big),
        uuid.UUID(ids["event"]),
    )

    body = (await account.client.get("/v1/live", params={"app_id": app["id"]})).json()
    event = next(item for item in body["items"] if item["kind"] == "event")
    summary = event["details"]["properties"]
    assert summary["_truncated"] is True
    assert "key_0" in summary["_keys"]
    assert "x" * 200 not in json.dumps(body)


async def test_a_viewer_cannot_read_the_live_feed(api_client, account):
    """Raw per-device events are operational data for people integrating, not a
    report. Viewers read aggregates; the feed needs member."""
    app = await _app(account, package="com.example.viewerlive")
    viewer = await register_account(api_client, name="Live Viewer")
    viewer_email = viewer.email
    await viewer.post("/v1/auth/logout")

    await api_client.post(
        "/v1/auth/login", json={"email": account.email, "password": account.password}
    )
    account.client.cookies = api_client.cookies
    added = await account.post(
        "/v1/organizations/members", json={"email": viewer_email, "role": "viewer"}
    )
    assert added.status_code == 201, added.text
    org_id = account.organization["id"]

    async with httpx.AsyncClient(transport=api_client._transport, base_url="http://api") as client:
        await client.post(
            "/v1/auth/login", json={"email": viewer_email, "password": viewer.password}
        )
        csrf = client.cookies["mmp_csrf"]
        await client.post(f"/v1/organizations/{org_id}/switch", headers={"x-csrf-token": csrf})
        response = await client.get("/v1/live", params={"app_id": app["id"]})
        assert response.status_code == 403


async def test_an_event_written_after_a_poll_is_not_lost_between_polls(account, owner_conn):
    """The failure the late-write margin exists for.

    An event carries the time the tracker received it but only appears once the
    worker writes it. Received just before a poll and written just after, it
    would fall between two polls with a strict ``since`` and never be shown — in
    the one view whose purpose is showing that an event arrived.
    """
    app = await _app(account, package="com.example.latewrite")
    first_poll = (await account.client.get("/v1/live", params={"app_id": app["id"]})).json()
    polled_at = dt.datetime.fromisoformat(first_poll["server_time"])

    # Received two seconds *before* that poll, written only now.
    late = await _seed(
        owner_conn, account.organization["id"], app["id"], at=polled_at - dt.timedelta(seconds=2)
    )

    second_poll = (
        await account.client.get(
            "/v1/live", params={"app_id": app["id"], "since": first_poll["server_time"]}
        )
    ).json()
    assert late["event"] in {item["id"] for item in second_poll["items"]}, (
        "an event written after the previous poll must still reach the next one"
    )


async def test_platforms_are_named_not_numbered(account, owner_conn):
    """Stored as a smallint. The first version of this view showed a click as
    "Live Campaign · 1", which is true and useless."""
    app = await _app(account, package="com.example.platformname")
    await _seed(owner_conn, account.organization["id"], app["id"])
    body = (await account.client.get("/v1/live", params={"app_id": app["id"]})).json()
    platforms = {item["kind"]: item.get("platform") for item in body["items"]}
    assert platforms["click"] == "android"
    assert platforms["event"] == "android"


async def test_a_sandbox_delivery_shows_the_request_it_would_have_sent(account, owner_conn):
    """Sandbox exists to show the rendered request. It was never sent, so there
    is no delivery to protect — only the URL to check."""
    app = await _app(account, package="com.example.pbsandbox")
    ids = await _seed(owner_conn, account.organization["id"], app["id"])
    await owner_conn.execute(
        "UPDATE postback_deliveries SET status = 'sandbox', response_status = NULL WHERE id = $1",
        uuid.UUID(ids["postback"]),
    )

    body = (await account.client.get("/v1/live", params={"app_id": app["id"]})).json()
    postback = next(item for item in body["items"] if item["kind"] == "postback")
    assert postback["status"] == "sandbox"
    assert postback["details"]["sandbox_request_url"] == (
        "https://net.example/pb?click=1&token=PARTNER-SECRET-TOKEN"
    )
