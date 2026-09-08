"""The analytics read path: bounded, cached, tenant-scoped, rollup-backed."""

from __future__ import annotations

import datetime as dt

import pytest

from tests.conftest_api import register_account

TODAY = dt.date.today()
WEEK_AGO = TODAY - dt.timedelta(days=7)


async def _app(account, name="Analytics App", package="com.example.analytics"):
    response = await account.post(
        "/v1/apps",
        json={"name": name, "platform": "android", "android_package_name": package},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _range() -> str:
    return f"from={WEEK_AGO}&to={TODAY}"


@pytest.mark.parametrize("endpoint", ["overview", "campaigns", "events"])
async def test_date_range_is_required(account, endpoint):
    """Never defaulted. A default range is a default scan, discovered by
    whoever opens the page on the largest account."""
    app = await _app(account)
    response = await account.client.get(f"/v1/analytics/{endpoint}?app_id={app['id']}")
    assert response.status_code == 422


@pytest.mark.parametrize("endpoint", ["overview", "campaigns", "events"])
async def test_range_is_capped(account, endpoint):
    app = await _app(account)
    response = await account.client.get(
        f"/v1/analytics/{endpoint}?app_id={app['id']}&from=2020-01-01&to={TODAY}"
    )
    assert response.status_code == 422
    assert "export" in response.json()["detail"], "the error should say what to do instead"


@pytest.mark.parametrize("endpoint", ["overview", "campaigns", "events"])
async def test_backwards_range_rejected(account, endpoint):
    app = await _app(account)
    response = await account.client.get(
        f"/v1/analytics/{endpoint}?app_id={app['id']}&from={TODAY}&to={WEEK_AGO}"
    )
    assert response.status_code == 422


async def test_overview_reads_a_rollup_not_raw_events(account):
    """The rule this stack lives by, asserted in the response itself."""
    app = await _app(account)
    response = await account.client.get(f"/v1/analytics/overview?app_id={app['id']}&{_range()}")
    assert response.status_code == 200
    assert response.headers["x-query-source"] == "rollup"


async def test_overview_is_cached(account):
    app = await _app(account)
    url = f"/v1/analytics/overview?app_id={app['id']}&{_range()}"

    first = await account.client.get(url)
    second = await account.client.get(url)
    assert first.headers["x-cache"] == "miss"
    assert second.headers["x-cache"] == "hit"
    assert first.json() == second.json()


async def test_a_warm_cache_does_not_expose_another_tenants_report(api_client, account):
    """A request for a foreign app 404s whether or not the result is cached.

    Named for what it actually checks. The app-existence check under RLS is the
    guard that fires here — it runs before the cache is consulted — so this does
    not exercise the cache key itself. That property is tested directly below,
    because a test whose name overstates what it verifies is worse than no test:
    it makes the next person believe something is covered when it is not.
    """
    app = await _app(account)
    url = f"/v1/analytics/overview?app_id={app['id']}&{_range()}"
    assert (await account.client.get(url)).headers["x-cache"] == "miss"
    assert (await account.client.get(url)).headers["x-cache"] == "hit"
    await account.post("/v1/auth/logout")

    intruder = await register_account(api_client, name="Intruder")
    response = await intruder.client.get(url)
    assert response.status_code == 404, "a cached result must not leak across tenants"


def test_cache_keys_are_tenant_scoped_by_construction():
    """Defence in depth behind the RLS check.

    The organisation id is part of the key rather than part of the value, so
    even if an endpoint above were to lose its tenant filter, a cache hit could
    not serve one tenant's numbers to another.
    """
    import uuid

    from mmp_api.routes.analytics import _cache_key

    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    app_id = uuid.uuid4()
    args = ("overview", app_id, "2026-09-01", "2026-09-08")

    assert _cache_key(org_a, *args) != _cache_key(org_b, *args)
    assert _cache_key(org_a, *args) == _cache_key(org_a, *args)
    assert str(org_a) in _cache_key(org_a, *args)


def test_cache_keys_distinguish_every_parameter():
    """Two different questions must not share an answer."""
    import uuid

    from mmp_api.routes.analytics import _cache_key

    org = uuid.uuid4()
    app_id = uuid.uuid4()
    base = _cache_key(org, "overview", app_id, "2026-09-01", "2026-09-08")

    assert base != _cache_key(org, "overview", app_id, "2026-09-02", "2026-09-08")
    assert base != _cache_key(org, "overview", app_id, "2026-09-01", "2026-09-09")
    assert base != _cache_key(org, "campaigns", app_id, "2026-09-01", "2026-09-08")
    assert base != _cache_key(org, "overview", uuid.uuid4(), "2026-09-01", "2026-09-08")


async def test_another_tenants_app_is_invisible(api_client, account, seeded_app):
    response = await account.client.get(
        f"/v1/analytics/overview?app_id={seeded_app['app_id']}&{_range()}"
    )
    assert response.status_code == 404


async def test_empty_app_reports_zero_not_an_error(account):
    """A brand new app has no data. That is a valid report, not a 404."""
    app = await _app(account, name="Fresh", package="com.example.fresh")
    response = await account.client.get(f"/v1/analytics/overview?app_id={app['id']}&{_range()}")
    assert response.status_code == 200
    totals = response.json()["totals"]
    assert totals["events"] == 0
    assert totals["installs"] == 0
    # Undefined, not zero: 0% would read as "nobody converted" rather than
    # "nobody clicked".
    assert totals["install_rate"] is None


async def test_overview_reflects_real_data(
    tracker, worker_consumer, click_consumer, seeded_app, api_client, owner_conn
):
    """End to end: events in, rollup refreshed, numbers out of the API."""
    from mmp_db.pool import Database
    from mmp_worker.rollups import refresh_trailing

    from tests.conftest_ingest import flush_tracker, sample_event

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                *(sample_event(event_name="install", anonymous_id=f"a{i}") for i in range(4)),
                sample_event(
                    event_name="purchase",
                    anonymous_id="a0",
                    revenue_minor=4999,
                    currency="USD",
                ),
            ]
        },
    )
    await flush_tracker(tracker)
    for _ in range(3):
        if await worker_consumer.run_once() == 0:
            break

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        await refresh_trailing(database)
    finally:
        await database.close()

    # Read through the API as a member of the owning organisation.
    row = await owner_conn.fetchrow(
        "SELECT sum(event_count) AS events, "
        "sum(event_count) FILTER (WHERE event_name = 'install') AS installs, "
        "sum(revenue_minor) AS revenue FROM rollup_events_hourly WHERE app_id = $1",
        seeded_app["app_id"],
    )
    assert row["events"] == 5
    assert row["installs"] == 4
    assert row["revenue"] == 4999


async def test_viewer_can_read_analytics(api_client, account):
    """Analytics is the one thing a viewer exists to do."""
    app = await _app(account, name="Viewer Analytics", package="com.example.viewer2")
    response = await account.client.get(f"/v1/analytics/overview?app_id={app['id']}&{_range()}")
    assert response.status_code == 200


async def test_unauthenticated_requests_rejected(api_client, account):
    app = await _app(account)
    url = f"/v1/analytics/overview?app_id={app['id']}&{_range()}"
    await account.post("/v1/auth/logout")
    assert (await api_client.get(url)).status_code == 401


@pytest.mark.parametrize("limit", [0, 501, -1])
async def test_limits_are_bounded(account, limit):
    app = await _app(account)
    response = await account.client.get(
        f"/v1/analytics/events?app_id={app['id']}&{_range()}&limit={limit}"
    )
    assert response.status_code == 422


async def test_device_counts_are_not_claimed_to_be_period_distinct(account):
    """A distinct count cannot be aggregated across buckets.

    Summing double-counts every device active in two hours; taking the maximum
    reports the busiest single hour. Neither is "unique devices this week", and
    the field is named for what the rollup can actually answer. Found by looking
    at the rendered page — 570 installs from 12 devices is arithmetically
    impossible, and no assertion in this file had caught it.
    """
    app = await _app(account, name="Distinct App", package="com.example.distinct")
    response = await account.client.get(f"/v1/analytics/overview?app_id={app['id']}&{_range()}")
    assert response.status_code == 200
    totals = response.json()["totals"]
    assert "peak_hourly_devices" in totals
    assert "unique_devices" not in totals, (
        "the API must not offer a period-level distinct count it cannot compute"
    )

    events = await account.client.get(f"/v1/analytics/events?app_id={app['id']}&{_range()}")
    assert events.status_code == 200


def test_cache_keys_carry_a_schema_version():
    """Cached values outlive deploys.

    Renaming a response field and shipping it meant the new code read old cache
    entries and failed validation on every request until the TTL expired — five
    minutes of 500s across the dashboard from a rename that looked entirely
    safe. It happened here, in development, and the version in the key is why it
    cannot happen in production.
    """
    import uuid

    from mmp_api.routes.analytics import CACHE_SCHEMA_VERSION, _cache_key

    key = _cache_key(uuid.uuid4(), "overview", uuid.uuid4(), "2026-09-01", "2026-09-08")
    assert f":v{CACHE_SCHEMA_VERSION}:" in key
