"""Every service boots, answers its probes, and stamps its responses."""

import httpx
import pytest
from mmp_api.app import create_app as create_api
from mmp_tracker.app import create_app as create_tracker
from mmp_web.app import create_app as create_web

SERVICES = [("tracker", create_tracker), ("api", create_api), ("web", create_web)]


@pytest.mark.parametrize(("name", "factory"), SERVICES, ids=[n for n, _ in SERVICES])
async def test_health_and_readiness(name, factory):
    app = factory()
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://t") as client,
    ):
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        ready = await client.get("/ready")
        assert ready.status_code == 200


@pytest.mark.parametrize(("name", "factory"), SERVICES, ids=[n for n, _ in SERVICES])
async def test_request_id_and_security_headers(name, factory):
    app = factory()
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://t") as client,
    ):
        response = await client.get("/health")
    assert response.headers["x-request-id"]
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_incoming_correlation_id_is_adopted():
    """A correlation ID from upstream must survive, so a trace stays one trace."""
    app = create_tracker()
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://t") as client,
    ):
        response = await client.get("/health", headers={"x-correlation-id": "trace-abc"})
    assert response.headers["x-correlation-id"] == "trace-abc"
