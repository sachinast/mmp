"""Roles, organisation switching, and cross-tenant access at the HTTP layer.

tests/test_tenant_isolation.py proves the database refuses cross-tenant reads.
This file proves the API does not hand someone a connection scoped to an
organisation they do not belong to in the first place.
"""

from __future__ import annotations

import httpx
import pytest

from tests.conftest_api import register_account


async def _make_app(account, name="Test App"):
    response = await account.post(
        "/v1/apps",
        json={
            "name": name,
            "platform": "android",
            "android_package_name": "com.example.testapp",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_admin_can_create_app(account):
    app = await _make_app(account)
    assert app["platform"] == "android"
    assert app["install_window_days"] == 7


async def test_package_name_is_validated(account):
    response = await account.post(
        "/v1/apps",
        json={"name": "Bad", "platform": "android", "android_package_name": "not a package"},
    )
    assert response.status_code == 422


async def test_platform_requires_matching_identifier(account):
    """An iOS app with no bundle ID can never be attributed. Reject it early."""
    response = await account.post("/v1/apps", json={"name": "No Bundle", "platform": "ios"})
    assert response.status_code == 422


async def test_attribution_window_bounds_are_enforced(account):
    response = await account.post(
        "/v1/apps",
        json={
            "name": "Wide",
            "platform": "android",
            "android_package_name": "com.example.wide",
            "install_window_days": 365,
        },
    )
    assert response.status_code == 422


async def test_another_organization_cannot_see_the_app(api_client, account):
    """The core multi-tenancy guarantee, end to end over HTTP."""
    app = await _make_app(account, "Private App")
    await account.post("/v1/auth/logout")

    intruder = await register_account(api_client, name="Intruder")
    listing = await intruder.client.get("/v1/apps")
    assert listing.status_code == 200
    assert app["id"] not in [a["id"] for a in listing.json()]

    direct = await intruder.client.get(f"/v1/apps/{app['id']}")
    assert direct.status_code == 404, "a foreign app must be invisible, not merely forbidden"


async def test_cannot_switch_into_an_organization_you_do_not_belong_to(api_client, account):
    """Otherwise 'switch' is a self-service grant to any organisation ID."""
    victim_org = account.organization["id"]
    await account.post("/v1/auth/logout")

    intruder = await register_account(api_client, name="Intruder")
    response = await intruder.post(f"/v1/organizations/{victim_org}/switch")
    assert response.status_code == 404


async def test_viewer_cannot_create_an_app(api_client, account):
    viewer = await register_account(api_client, name="Viewer")
    viewer_email = viewer.email
    await viewer.post("/v1/auth/logout")

    owner = await api_client.post(
        "/v1/auth/login", json={"email": account.email, "password": account.password}
    )
    assert owner.status_code == 200
    account.client.cookies = api_client.cookies
    added = await account.post(
        "/v1/organizations/members", json={"email": viewer_email, "role": "viewer"}
    )
    assert added.status_code == 201, added.text
    org_id = account.organization["id"]
    await account.post("/v1/auth/logout")

    async with httpx.AsyncClient(transport=api_client._transport, base_url="http://api") as client:
        login = await client.post(
            "/v1/auth/login", json={"email": viewer_email, "password": viewer.password}
        )
        assert login.status_code == 200
        csrf = client.cookies["mmp_csrf"]
        switched = await client.post(
            f"/v1/organizations/{org_id}/switch", headers={"x-csrf-token": csrf}
        )
        assert switched.status_code == 200

        created = await client.post(
            "/v1/apps",
            json={
                "name": "Viewer App",
                "platform": "android",
                "android_package_name": "com.example.viewer",
            },
            headers={"x-csrf-token": csrf},
        )
        assert created.status_code == 403

        listed = await client.get("/v1/apps")
        assert listed.status_code == 200, "a viewer must still be able to read"


async def test_admin_cannot_grant_a_role_above_their_own(api_client, account):
    """Otherwise 'admin' is just 'owner' with one extra API call."""
    target = await register_account(api_client, name="Target")
    target_email = target.email
    await target.post("/v1/auth/logout")

    await api_client.post(
        "/v1/auth/login", json={"email": account.email, "password": account.password}
    )
    account.client.cookies = api_client.cookies
    added = await account.post(
        "/v1/organizations/members", json={"email": target_email, "role": "admin"}
    )
    assert added.status_code == 201
    org_id = account.organization["id"]
    await account.post("/v1/auth/logout")

    third = await register_account(api_client, name="Third")
    third_email = third.email
    await third.post("/v1/auth/logout")

    async with httpx.AsyncClient(transport=api_client._transport, base_url="http://api") as client:
        await client.post(
            "/v1/auth/login", json={"email": target_email, "password": target.password}
        )
        csrf = client.cookies["mmp_csrf"]
        await client.post(f"/v1/organizations/{org_id}/switch", headers={"x-csrf-token": csrf})
        escalation = await client.post(
            "/v1/organizations/members",
            json={"email": third_email, "role": "owner"},
            headers={"x-csrf-token": csrf},
        )
        assert escalation.status_code == 403


async def test_last_owner_cannot_be_removed(account):
    """An organisation with no owner can never grant access to itself again."""
    me = await account.client.get("/v1/auth/me")
    response = await account.delete(f"/v1/organizations/members/{me.json()['id']}")
    assert response.status_code == 409


async def test_revoked_membership_takes_effect_on_the_next_request(api_client, account):
    """Authorisation is re-derived per request, never cached in the session."""
    member = await register_account(api_client, name="Member")
    member_email, member_password = member.email, member.password
    await member.post("/v1/auth/logout")

    await api_client.post(
        "/v1/auth/login", json={"email": account.email, "password": account.password}
    )
    account.client.cookies = api_client.cookies
    await account.post("/v1/organizations/members", json={"email": member_email, "role": "member"})
    org_id = account.organization["id"]
    owner_cookies = api_client.cookies.jar

    async with httpx.AsyncClient(transport=api_client._transport, base_url="http://api") as client:
        await client.post(
            "/v1/auth/login", json={"email": member_email, "password": member_password}
        )
        csrf = client.cookies["mmp_csrf"]
        await client.post(f"/v1/organizations/{org_id}/switch", headers={"x-csrf-token": csrf})
        assert (await client.get("/v1/apps")).status_code == 200

        member_id = (await client.get("/v1/auth/me")).json()["id"]

        # Owner removes them while their session is still live.
        assert owner_cookies is not None
        await account.delete(f"/v1/organizations/members/{member_id}")

        after = await client.get("/v1/apps")
        assert after.status_code == 403, (
            "a removed member must lose access immediately, not when their session expires"
        )


@pytest.mark.parametrize("method,path", [("get", "/v1/apps"), ("get", "/v1/organizations/members")])
async def test_no_active_organization_is_a_clear_error(api_client, method, path):
    """A user with a session but no tenant must not silently see everything."""
    assert (await getattr(api_client, method)(path)).status_code == 401
