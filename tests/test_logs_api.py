"""The logs: raw rows with filters and keyset pagination."""

from __future__ import annotations

import datetime as dt
import uuid

from mmp_core.ids import uuid7

from tests.conftest_api import register_account

TODAY = dt.datetime.now(dt.UTC).date()


async def _app(account):
    response = await account.post(
        "/v1/apps",
        json={"name": "Logs App", "platform": "android", "android_package_name": "com.ex.logs"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _seed_events(owner_conn, org_id: str, app_id: str, count: int) -> None:
    now = dt.datetime.now(dt.UTC)
    for index in range(count):
        await owner_conn.execute(
            "INSERT INTO events (event_id, received_at, occurred_at, organization_id, app_id,"
            " event_name, anonymous_id, user_id, platform, revenue_minor, currency, properties)"
            ' VALUES ($1, $2, $2, $3, $4, $5, $6, $7, $8, $9, $10, \'{"sku": "A-1"}\')',
            uuid7(),
            now - dt.timedelta(seconds=index),
            uuid.UUID(org_id),
            uuid.UUID(app_id),
            "purchase" if index % 2 else "add_to_cart",
            f"device-{index % 3}",
            "user-42" if index == 0 else None,
            1 if index % 4 else 2,
            499 if index % 2 else None,
            "USD" if index % 2 else None,
        )


async def test_events_log_filters_and_pages(api_client, owner_conn):
    account = await register_account(api_client)
    app = await _app(account)
    await _seed_events(owner_conn, account.organization["id"], app["id"], 7)
    base = f"/v1/logs/events?app_id={app['id']}&from={TODAY - dt.timedelta(days=1)}&to={TODAY}"

    first = await account.client.get(f"{base}&limit=3")
    assert first.status_code == 200, first.text
    page = first.json()
    assert len(page["items"]) == 3
    assert page["next_cursor"], "seven rows and a page of three means there is more"
    assert page["items"][0]["user_id"] == "user-42", "newest first"
    assert page["items"][0]["properties"] == {"sku": "A-1"}
    assert "device_hash" not in page["items"][0] and "ip_hash" not in page["items"][0]

    second = await account.client.get(f"{base}&limit=3&cursor={page['next_cursor']}")
    assert second.status_code == 200
    seen = {item["event_id"] for item in page["items"]}
    assert not seen & {item["event_id"] for item in second.json()["items"]}, "no overlap"

    third = await account.client.get(f"{base}&limit=3&cursor={second.json()['next_cursor']}")
    assert len(third.json()["items"]) == 1
    assert third.json()["next_cursor"] is None

    by_name = await account.client.get(f"{base}&event_name=purchase")
    assert {item["event_name"] for item in by_name.json()["items"]} == {"purchase"}
    assert all(item["revenue_minor"] == 499 for item in by_name.json()["items"])

    by_device = await account.client.get(f"{base}&anonymous_id=device-1")
    assert {item["anonymous_id"] for item in by_device.json()["items"]} == {"device-1"}

    by_platform = await account.client.get(f"{base}&platform=ios")
    assert {item["platform"] for item in by_platform.json()["items"]} == {"ios"}


async def test_logs_refuse_a_range_over_a_month_and_a_bad_cursor(api_client):
    account = await register_account(api_client)
    app = await _app(account)
    too_wide = await account.client.get(
        f"/v1/logs/events?app_id={app['id']}&from={TODAY - dt.timedelta(days=60)}&to={TODAY}"
    )
    assert too_wide.status_code == 422
    bad_cursor = await account.client.get(
        f"/v1/logs/clicks?app_id={app['id']}&from={TODAY}&to={TODAY}&cursor=not-a-cursor"
    )
    assert bad_cursor.status_code == 422
    unknown_platform = await account.client.get(
        f"/v1/logs/events?app_id={app['id']}&from={TODAY}&to={TODAY}&platform=blackberry"
    )
    assert unknown_platform.status_code == 422


async def test_logs_are_tenant_scoped(api_client, owner_conn):
    mine = await register_account(api_client)
    app = await _app(mine)
    await _seed_events(owner_conn, mine.organization["id"], app["id"], 2)
    empty_clicks = await mine.client.get(
        f"/v1/logs/clicks?app_id={app['id']}&from={TODAY}&to={TODAY}"
    )
    assert empty_clicks.status_code == 200
    assert empty_clicks.json() == {"items": [], "next_cursor": None}
    empty_installs = await mine.client.get(
        f"/v1/logs/installs?app_id={app['id']}&from={TODAY}&to={TODAY}"
    )
    assert empty_installs.json() == {"items": [], "next_cursor": None}

    # Registering a second account signs the shared client in as them — which
    # is the point: the same client, a different tenant, and the app vanishes.
    theirs = await register_account(api_client)
    response = await theirs.client.get(
        f"/v1/logs/events?app_id={app['id']}&from={TODAY}&to={TODAY}"
    )
    assert response.status_code == 404
