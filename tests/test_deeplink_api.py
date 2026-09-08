"""The deep link registry API."""

from __future__ import annotations


async def _app(account, name="DL App", package="com.example.dl"):
    response = await account.post(
        "/v1/apps",
        json={"name": name, "platform": "android", "android_package_name": package},
    )
    return response.json()


def _body(app, **overrides):
    return {
        "app_id": app["id"],
        "code": "summer",
        "destination": "/campaigns/summer",
        "fallback_url": "https://example.com/summer",
        **overrides,
    }


async def test_create_and_list(account):
    app = await _app(account)
    created = await account.post("/v1/deep-links", json=_body(app))
    assert created.status_code == 201, created.text
    assert created.json()["destination"] == "/campaigns/summer"

    listing = await account.client.get(f"/v1/deep-links?app_id={app['id']}")
    assert [d["code"] for d in listing.json()] == ["summer"]


async def test_a_custom_scheme_is_allowed_from_the_advertiser(account):
    """The registry is exactly the place a scheme is acceptable: it came from an
    authenticated advertiser, not from a URL someone tapped."""
    app = await _app(account, name="Scheme", package="com.example.scheme")
    created = await account.post("/v1/deep-links", json=_body(app, destination="myapp://product/1"))
    assert created.status_code == 201


async def test_a_multiline_destination_is_refused(account):
    """It is echoed into a redirect's Location further down the path."""
    app = await _app(account, name="Multi", package="com.example.multi")
    response = await account.post(
        "/v1/deep-links",
        json=_body(app, destination="/ok\r\nLocation: https://evil.example"),
    )
    assert response.status_code == 422


async def test_a_bad_code_is_refused(account):
    app = await _app(account, name="Bad", package="com.example.bad")
    response = await account.post("/v1/deep-links", json=_body(app, code="../etc"))
    assert response.status_code == 422


async def test_a_duplicate_code_for_one_app_is_a_conflict(account):
    app = await _app(account, name="Dup", package="com.example.dup")
    assert (await account.post("/v1/deep-links", json=_body(app))).status_code == 201
    second = await account.post("/v1/deep-links", json=_body(app, destination="/other"))
    assert second.status_code == 409


async def test_another_tenants_app_is_not_found(api_client, account):
    """Not 403. A tenant must not be able to learn that an app id exists."""
    app = await _app(account, name="Mine", package="com.example.mineonly")

    from tests.conftest_api import register_account

    other = await register_account(api_client, name="Other")
    response = await other.post("/v1/deep-links", json=_body(app))
    assert response.status_code == 404


async def test_delete_removes_it(account):
    app = await _app(account, name="Del", package="com.example.del")
    created = await account.post("/v1/deep-links", json=_body(app))
    deep_link_id = created.json()["id"]

    assert (await account.delete(f"/v1/deep-links/{deep_link_id}")).status_code == 204
    listing = await account.client.get(f"/v1/deep-links?app_id={app['id']}")
    assert listing.json() == []
