"""Every service boots, answers its probes, and stamps its responses."""

import httpx
import pytest
from mmp_api.app import create_app as create_api
from mmp_tracker.app import create_app as create_tracker
from mmp_web.app import create_app as create_web

from tests.conftest_api import build_settings_for


def _create_api():
    # The API and the tracker open real connection pools at startup, so they
    # need the test database — and each must connect as the role it uses in
    # production, since their grants and RLS policies differ.
    return create_api(build_settings_for("mmp_api"))


def _create_tracker():
    return create_tracker(build_settings_for("mmp_tracker"))


SERVICES = [("tracker", _create_tracker), ("api", _create_api), ("web", create_web)]


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
    app = _create_tracker()
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://t") as client,
    ):
        response = await client.get("/health", headers={"x-correlation-id": "trace-abc"})
    assert response.headers["x-correlation-id"] == "trace-abc"


async def test_the_api_root_points_somewhere_useful(api_client):
    """A bare 404 at the root is correct and unhelpful. The first thing someone
    does after one is check whether they got the port wrong."""
    response = await api_client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "api"
    assert body["documentation"] == "/docs"


async def test_the_tracker_root_does_not_enumerate_its_ingest_surface(tracker):
    """The tracker is the service on the public internet. It names itself so a
    developer is not lost, and stops there — listing its endpoints for an
    anonymous caller who has not been given a key buys nothing."""
    response = await tracker.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "tracker"
    for path in ("/v1/events", "/v1/s2s/events", "/v1/deeplink/resolve", "/c/"):
        assert path not in response.text, f"the root advertises {path}"
