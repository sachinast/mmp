"""Dashboard fixtures.

The web service is run against a **real API**, not a mock. Almost everything
worth asserting here — session forwarding, the login redirect, tenant scoping,
the error path — only exists once the two services are actually talking.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

from tests.conftest_api import build_api_settings, build_settings_for


@pytest_asyncio.fixture
async def web_stack(api_client: httpx.AsyncClient) -> AsyncIterator[dict]:
    """The dashboard, wired to an in-process API over an ASGI transport.

    The web app's httpx calls are redirected at the transport layer so no port
    is bound, while every layer above the socket — cookies, headers, status
    codes, the API's real authorisation — runs for real.
    """
    from mmp_web.app import create_app as create_web

    api_settings = build_settings_for("mmp_api")
    web_settings = build_api_settings().model_copy(
        update={
            "service_name": "web-test",
            "api_base_url": "http://api.test",
            "tracking_domain": "https://track.test",
            "shutdown_grace_seconds": 0.0,
        }
    )
    del api_settings  # the api_client fixture already built the API

    api_transport = api_client._transport

    import mmp_web.app as web_module
    import mmp_web.client as client_module

    original_client = httpx.AsyncClient

    class RoutedClient(httpx.AsyncClient):
        """Every outbound call from the dashboard lands on the test API."""

        def __init__(self, *args, **kwargs):
            kwargs["transport"] = api_transport
            super().__init__(*args, **kwargs)

    client_module.httpx.AsyncClient = RoutedClient  # type: ignore[misc]
    web_module.httpx.AsyncClient = RoutedClient  # type: ignore[misc]

    app = create_web(web_settings)
    transport = httpx.ASGITransport(app=app)
    try:
        async with (
            app.router.lifespan_context(app),
            original_client(transport=transport, base_url="http://web.test") as client,
        ):
            yield {"client": client, "app": app}
    finally:
        client_module.httpx.AsyncClient = original_client  # type: ignore[misc]
        web_module.httpx.AsyncClient = original_client  # type: ignore[misc]


@pytest_asyncio.fixture
async def signed_in(web_stack, api_client) -> dict:
    """A registered account, signed in through the dashboard's own login form."""
    email = f"web-{secrets.token_hex(6)}@example.com"
    password = "correct-horse-battery-staple"

    registration = await api_client.post(
        "/v1/auth/register",
        json={
            "email": email,
            "password": password,
            "name": "Web User",
            "organization_name": f"Web Org {secrets.token_hex(3)}",
        },
    )
    assert registration.status_code == 201, registration.text
    api_client.cookies.clear()

    web = web_stack["client"]
    response = await web.post("/login", data={"email": email, "password": password, "next": "/"})
    assert response.status_code == 303, response.text
    return {**web_stack, "email": email, "password": password}


@pytest.fixture
def web(web_stack) -> httpx.AsyncClient:
    return web_stack["client"]
