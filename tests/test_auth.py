"""Authentication: sessions, CSRF, rate limiting, enumeration resistance."""

from __future__ import annotations

import pytest

from tests.conftest_api import register_account


async def test_register_creates_session_and_org(api_client):
    account = await register_account(api_client)
    assert account.organization["role"] == "owner"
    me = await api_client.get("/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == account.email


async def test_session_cookie_is_httponly_and_samesite(api_client):
    """The session cookie must be unreadable from JavaScript.

    Asserted on the Set-Cookie header directly, because a cookie jar does not
    expose these flags — and they are the entire reason an XSS bug in the
    dashboard would not hand over an organisation's session.
    """
    import secrets

    response = await api_client.post(
        "/v1/auth/register",
        json={
            "email": f"flags-{secrets.token_hex(4)}@example.com",
            "password": "correct-horse-battery-staple",
            "name": "Flags",
            "organization_name": "Flags Org",
        },
    )
    assert response.status_code == 201

    set_cookies = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in set_cookies if c.startswith("mmp_session="))
    assert "HttpOnly" in session_cookie
    assert "SameSite=lax" in session_cookie.replace("samesite=lax", "SameSite=lax")

    # The CSRF cookie is the deliberate exception: the dashboard's own script
    # must read it to echo it back in a header.
    csrf_cookie = next(c for c in set_cookies if c.startswith("mmp_csrf="))
    assert "HttpOnly" not in csrf_cookie


async def test_login_succeeds_and_me_reflects_user(api_client):
    account = await register_account(api_client)
    await account.post("/v1/auth/logout")
    response = await api_client.post(
        "/v1/auth/login", json={"email": account.email, "password": account.password}
    )
    assert response.status_code == 200
    assert response.json()["email"] == account.email


async def test_wrong_password_is_rejected(api_client):
    account = await register_account(api_client)
    await account.post("/v1/auth/logout")
    response = await api_client.post(
        "/v1/auth/login", json={"email": account.email, "password": "not-the-password"}
    )
    assert response.status_code == 401


async def test_unknown_and_known_emails_give_the_same_answer(api_client):
    """No account-existence oracle.

    A different status or message for "no such user" versus "wrong password"
    lets anyone enumerate which of their competitors' emails have accounts.
    """
    account = await register_account(api_client)
    await account.post("/v1/auth/logout")

    unknown = await api_client.post(
        "/v1/auth/login",
        json={"email": "definitely-not-registered@example.com", "password": "whatever-12345"},
    )
    known = await api_client.post(
        "/v1/auth/login", json={"email": account.email, "password": "wrong-password-here"}
    )
    assert unknown.status_code == known.status_code == 401
    assert unknown.json()["detail"] == known.json()["detail"]


async def test_duplicate_registration_does_not_confirm_the_email(api_client):
    account = await register_account(api_client)
    response = await api_client.post(
        "/v1/auth/register",
        json={
            "email": account.email,
            "password": "another-valid-password",
            "name": "Impostor",
            "organization_name": "Elsewhere",
        },
    )
    assert response.status_code == 409
    assert "already" not in response.json()["detail"].lower()


async def test_logout_actually_revokes(api_client):
    account = await register_account(api_client)
    assert (await api_client.get("/v1/auth/me")).status_code == 200
    await account.post("/v1/auth/logout")
    assert (await api_client.get("/v1/auth/me")).status_code == 401


async def test_logout_everywhere_kills_other_sessions(api_client):
    """The property a JWT cannot provide."""
    import httpx

    account = await register_account(api_client)

    # A second, independent session for the same user.
    transport = api_client._transport
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as second:
        login = await second.post(
            "/v1/auth/login", json={"email": account.email, "password": account.password}
        )
        assert login.status_code == 200
        assert (await second.get("/v1/auth/me")).status_code == 200

        await account.post("/v1/auth/logout-everywhere")

        assert (await second.get("/v1/auth/me")).status_code == 401, (
            "revoking all sessions must invalidate sessions other than the caller's"
        )


async def test_state_change_requires_csrf_token(api_client):
    """A cross-origin form post carries the cookie but cannot read the token."""
    account = await register_account(api_client)
    without = await api_client.post("/v1/organizations", json={"name": "No CSRF"})
    assert without.status_code == 403

    with_token = await account.post("/v1/organizations", json={"name": "With CSRF"})
    assert with_token.status_code == 201


async def test_wrong_csrf_token_is_rejected(api_client):
    await register_account(api_client)
    response = await api_client.post(
        "/v1/organizations",
        json={"name": "Forged"},
        headers={"x-csrf-token": "not-the-right-token"},
    )
    assert response.status_code == 403


async def test_reads_do_not_require_csrf(api_client):
    """CSRF protects state changes; requiring it on GET would break links."""
    await register_account(api_client)
    assert (await api_client.get("/v1/auth/me")).status_code == 200


async def test_login_is_rate_limited_per_account(api_client):
    account = await register_account(api_client)
    await account.post("/v1/auth/logout")

    statuses = []
    for _ in range(8):
        response = await api_client.post(
            "/v1/auth/login", json={"email": account.email, "password": "wrong"}
        )
        statuses.append(response.status_code)

    assert 429 in statuses, "brute force must be throttled"
    limited = next(s for s in statuses if s == 429)
    assert limited == 429


async def test_short_password_rejected(api_client):
    import secrets

    response = await api_client.post(
        "/v1/auth/register",
        json={
            "email": f"short-{secrets.token_hex(4)}@example.com",
            "password": "short",
            "name": "X",
            "organization_name": "Y",
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize("path", ["/v1/auth/me", "/v1/organizations", "/v1/apps"])
async def test_endpoints_require_authentication(api_client, path):
    assert (await api_client.get(path)).status_code == 401
