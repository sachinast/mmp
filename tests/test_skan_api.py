"""SKAdNetwork configuration and reporting."""

from __future__ import annotations

import datetime as dt

from mmp_core.ids import uuid7

from tests.test_skan_endpoint import APPLE_APP_ID


async def _app(account, name="SKAN App", package="com.example.skan"):
    response = await account.post(
        "/v1/apps",
        json={"name": name, "platform": "ios", "ios_bundle_id": package},
    )
    return response.json()


def _range(days=7):
    now = dt.datetime.now(dt.UTC)
    return {
        "since": (now - dt.timedelta(days=days)).isoformat(),
        "until": (now + dt.timedelta(minutes=5)).isoformat(),
    }


def _mapping(app, **overrides):
    return {
        "app_id": app["id"],
        "event_name": "purchase",
        "conversion_value": 20,
        **overrides,
    }


async def test_a_conversion_value_can_be_set_and_listed(account):
    app = await _app(account)
    created = await account.put("/v1/skan/conversion-values", json=_mapping(app))
    assert created.status_code == 200, created.text
    assert created.json()["conversion_value"] == 20

    listing = await account.client.get(f"/v1/skan/conversion-values?app_id={app['id']}")
    assert [m["event_name"] for m in listing.json()] == ["purchase"]


async def test_setting_the_same_event_twice_updates_rather_than_conflicts(account):
    """An advertiser tunes this repeatedly while working out what their 64
    values mean. A create-only endpoint would force delete-then-create, leaving
    a window where the event maps to nothing."""
    app = await _app(account, name="Tune", package="com.example.tune")
    first = await account.put("/v1/skan/conversion-values", json=_mapping(app))
    second = await account.put(
        "/v1/skan/conversion-values", json=_mapping(app, conversion_value=35)
    )

    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"], "the same mapping, updated"
    listing = await account.client.get(f"/v1/skan/conversion-values?app_id={app['id']}")
    assert len(listing.json()) == 1
    assert listing.json()[0]["conversion_value"] == 35


async def test_a_value_outside_apples_six_bits_is_refused(account):
    """The range is Apple's: a conversion value is six bits and nothing else."""
    app = await _app(account, name="Range", package="com.example.range")
    for value in (-1, 64, 1000):
        response = await account.put(
            "/v1/skan/conversion-values", json=_mapping(app, conversion_value=value)
        )
        assert response.status_code == 422, f"{value} should be refused"


async def test_an_invalid_coarse_value_is_refused(account):
    app = await _app(account, name="Coarse", package="com.example.coarse")
    response = await account.put(
        "/v1/skan/conversion-values", json=_mapping(app, coarse_value="enormous")
    )
    assert response.status_code == 422


async def test_another_tenants_app_is_not_found(api_client, account):
    app = await _app(account, name="Mine", package="com.example.skanmine")

    from tests.conftest_api import register_account

    other = await register_account(api_client, name="Other")
    response = await other.put("/v1/skan/conversion-values", json=_mapping(app))
    assert response.status_code == 404


async def _postback(owner_conn, app_id, org_id, **overrides):
    values = {
        "did_win": True,
        "conversion_value": 20,
        "ad_network_id": "example123.skadnetwork",
        "source_identifier": "42",
        "redownload": False,
        **overrides,
    }
    await owner_conn.execute(
        """INSERT INTO skadnetwork_postbacks (id, organization_id, app_id, version,
               ad_network_id, apple_app_id, transaction_id, source_identifier, did_win,
               redownload, conversion_value, payload)
           VALUES ($1, $2, $3, '4.0', $4, $5, $6, $7, $8, $9, $10, '{}'::jsonb)""",
        uuid7(),
        org_id,
        app_id,
        values["ad_network_id"],
        APPLE_APP_ID,
        str(uuid7()),
        values["source_identifier"],
        values["did_win"],
        values["redownload"],
        values["conversion_value"],
    )


async def test_postbacks_are_listed_and_summarised(account, owner_conn):
    app = await _app(account, name="Report", package="com.example.report")
    org_id = await owner_conn.fetchval("SELECT organization_id FROM apps WHERE id = $1", app["id"])

    await _postback(owner_conn, app["id"], org_id, conversion_value=10)
    await _postback(owner_conn, app["id"], org_id, conversion_value=30)
    await _postback(owner_conn, app["id"], org_id, did_win=False, conversion_value=None)

    listing = await account.client.get(
        "/v1/skan/postbacks", params={"app_id": app["id"], **_range()}
    )
    assert listing.status_code == 200
    assert len(listing.json()) == 3

    summary = await account.client.get("/v1/skan/summary", params={"app_id": app["id"], **_range()})
    row = summary.json()["rows"][0]
    assert row["winning_postbacks"] == 2
    assert row["non_winning_postbacks"] == 1
    assert row["average_conversion_value"] == 20.0


async def test_withheld_conversion_values_are_counted_not_hidden(account, owner_conn):
    """Apple nulls the conversion value below its privacy thresholds. An average
    computed over an unstated fraction of the data is exactly the number that
    gets quoted without its denominator."""
    app = await _app(account, name="Suppressed", package="com.example.suppressed")
    org_id = await owner_conn.fetchval("SELECT organization_id FROM apps WHERE id = $1", app["id"])

    await _postback(owner_conn, app["id"], org_id, conversion_value=40)
    for _ in range(3):
        await _postback(owner_conn, app["id"], org_id, conversion_value=None)

    summary = await account.client.get("/v1/skan/summary", params={"app_id": app["id"], **_range()})
    row = summary.json()["rows"][0]
    assert row["winning_postbacks"] == 4
    assert row["suppressed"] == 3, "the withheld values must be visible in the response"
    assert row["average_conversion_value"] == 40.0


async def test_the_summary_states_that_it_does_not_reconcile(account):
    """SKAdNetwork numbers cannot be compared with deterministic installs. The
    caveat travels with the data rather than living in a dashboard someone may
    reimplement."""
    app = await _app(account, name="Caveat", package="com.example.caveat")
    summary = await account.client.get("/v1/skan/summary", params={"app_id": app["id"], **_range()})
    assert "not comparable" in summary.json()["caveat"]


async def test_an_unbounded_report_range_is_refused(account):
    app = await _app(account, name="Wide", package="com.example.wide")
    response = await account.client.get(
        "/v1/skan/postbacks", params={"app_id": app["id"], **_range(days=200)}
    )
    assert response.status_code == 422


async def test_postbacks_do_not_cross_a_tenant(api_client, account, owner_conn):
    app = await _app(account, name="Isolated", package="com.example.isolated")
    org_id = await owner_conn.fetchval("SELECT organization_id FROM apps WHERE id = $1", app["id"])
    await _postback(owner_conn, app["id"], org_id)

    own = await account.client.get("/v1/skan/postbacks", params={"app_id": app["id"], **_range()})
    assert len(own.json()) == 1, "the owner must actually see it"

    from tests.conftest_api import register_account

    other = await register_account(api_client, name="Other Tenant")
    theirs = await other.client.get("/v1/skan/postbacks", params={"app_id": app["id"], **_range()})
    assert theirs.json() == []
