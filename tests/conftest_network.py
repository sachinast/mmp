"""A real HTTP endpoint for postback tests to deliver to.

Shared through ``pytest_plugins`` rather than imported: more than one test module
delivers postbacks, and importing fixtures from a test module makes every test
that uses them look, to a linter, like it redefines a name.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from urllib.parse import parse_qs

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route


@dataclass
class Network:
    """An ad network's conversion endpoint."""

    url: str = ""
    received: list[dict] = field(default_factory=list)
    respond_with: int = 200
    body: str = "ok"


@pytest.fixture
async def network():
    import uvicorn

    state = Network()

    async def handle(request: Request) -> Response:
        state.received.append(
            {
                "query": dict(parse_qs(request.url.query)),
                "path": request.url.path,
                "method": request.method,
                "headers": dict(request.headers),
                "body": (await request.body()).decode(),
            }
        )
        return PlainTextResponse(state.body, status_code=state.respond_with)

    app = Starlette(routes=[Route("/conv", handle, methods=["GET", "POST"])])
    config = uvicorn.Config(app, host="127.0.0.1", port=8978, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    state.url = "http://127.0.0.1:8978/conv"
    try:
        yield state
    finally:
        server.should_exit = True
        await task


@pytest.fixture
def allow_loopback(monkeypatch):
    """Let the outbound client reach the test server.

    Scoped to these tests, and it replaces only the destination check — the
    request itself still goes through the real client. The guard stays under
    test everywhere else.
    """
    from mmp_core import outbound

    monkeypatch.setattr(
        outbound,
        "validate_destination",
        lambda url, allow_http=False: outbound.ResolvedTarget(
            url=url, hostname="127.0.0.1", address="127.0.0.1", port=8978
        ),
    )
