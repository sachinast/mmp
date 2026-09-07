"""API fixtures: a real app against the real test database and Redis."""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from mmp_core.settings import Settings
from redis.asyncio import Redis

from tests.conftest_db import owner_dsn, role_dsn

TEST_REDIS_URL = os.environ.get("MMP_TEST_REDIS_URL", "redis://127.0.0.1:6379/15")


def build_settings_for(role: str) -> Settings:
    """Settings whose DSN connects as one specific application role.

    Which role a service connects as is a security property, not a detail:
    mmp_tracker can read three tables and insert into two, while mmp_api is
    subject to tenant isolation on everything. Tests that connect as the wrong
    role prove nothing about the real deployment.
    """
    settings = build_api_settings()
    dsn = role_dsn(role).replace("postgresql://", "postgresql+asyncpg://")
    return settings.model_copy(update={"database_url": dsn, "service_name": role})


def build_api_settings() -> Settings:
    # Connect as mmp_api, the role the service actually uses in production.
    #
    # This is not a detail. A superuser — which a developer's local Postgres
    # account usually is — bypasses row-level security entirely, FORCE included.
    # Running these tests as the owner made every cross-tenant assertion below
    # pass vacuously while the isolation they describe was never exercised. The
    # first version of this fixture did exactly that, and the cross-tenant test
    # caught it.
    api_dsn = role_dsn("mmp_api").replace("postgresql://", "postgresql+asyncpg://")
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="dev",
        service_name="api-test",
        database_url=api_dsn,
        redis_url=TEST_REDIS_URL,
        api_key_pepper=secrets.token_hex(32),
        ip_hash_pepper=secrets.token_hex(32),
        session_secret=secrets.token_hex(32),
        shutdown_grace_seconds=0.0,
        log_json=False,
    )


@pytest_asyncio.fixture
async def api_client() -> AsyncIterator[httpx.AsyncClient]:
    """A client bound to a fully wired app, with its own Redis database.

    Deliberately not a mocked app. Almost everything asserted here — RLS
    scoping, CSRF, rate limiting, session revocation — only exists once the
    real dependencies are in the path.
    """
    import asyncpg

    try:
        conn = await asyncpg.connect(owner_dsn(), timeout=2)
        await conn.close()
        redis = Redis.from_url(TEST_REDIS_URL)
        await redis.ping()
        await redis.flushdb()
        await redis.aclose()
    except Exception:
        pytest.skip("postgres or redis not reachable for API tests")

    from mmp_api.app import create_app

    # The API connects as the schema owner in tests so that fixtures can set up
    # data; RLS enforcement itself is covered separately in
    # tests/test_tenant_isolation.py, where the connection is a real app role.
    app = create_app(build_api_settings())
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://api") as client,
    ):
        yield client


class Account:
    """A registered user with an active session and its CSRF token."""

    def __init__(self, client: httpx.AsyncClient, email: str, password: str, org: dict) -> None:
        self.client = client
        self.email = email
        self.password = password
        self.organization = org

    @property
    def csrf(self) -> str:
        return self.client.cookies["mmp_csrf"]

    def headers(self) -> dict[str, str]:
        return {"x-csrf-token": self.csrf}

    async def post(self, url: str, **kwargs) -> httpx.Response:
        headers = {**kwargs.pop("headers", {}), **self.headers()}
        return await self.client.post(url, headers=headers, **kwargs)

    async def patch(self, url: str, **kwargs) -> httpx.Response:
        headers = {**kwargs.pop("headers", {}), **self.headers()}
        return await self.client.patch(url, headers=headers, **kwargs)

    async def delete(self, url: str, **kwargs) -> httpx.Response:
        headers = {**kwargs.pop("headers", {}), **self.headers()}
        return await self.client.delete(url, headers=headers, **kwargs)


async def register_account(client: httpx.AsyncClient, *, name: str = "Test User") -> Account:
    email = f"user-{secrets.token_hex(6)}@example.com"
    password = "correct-horse-battery-staple"
    response = await client.post(
        "/v1/auth/register",
        json={
            "email": email,
            "password": password,
            "name": name,
            "organization_name": f"Org {secrets.token_hex(3)}",
        },
    )
    assert response.status_code == 201, response.text
    return Account(client, email, password, response.json())


@pytest_asyncio.fixture
async def account(api_client: httpx.AsyncClient) -> Account:
    return await register_account(api_client)
