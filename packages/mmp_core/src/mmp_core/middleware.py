"""ASGI middleware shared by every service."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mmp_core.context import (
    CORRELATION_ID_HEADER,
    REQUEST_ID_HEADER,
    correlation_id_var,
    new_id,
    request_id_var,
)
from mmp_core.logging import get_logger

log = get_logger(__name__)


class RequestContextMiddleware:
    """Assign a request ID, adopt or mint a correlation ID, log the outcome.

    Written as raw ASGI rather than ``BaseHTTPMiddleware`` because the tracker's
    latency budget cannot absorb the extra task and anyio stream that
    ``BaseHTTPMiddleware`` puts in the path of every request.
    """

    def __init__(self, app: ASGIApp, *, access_log: bool = True) -> None:
        self.app = app
        self.access_log = access_log

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        request_id = headers.get(REQUEST_ID_HEADER) or new_id()
        correlation_id = headers.get(CORRELATION_ID_HEADER) or request_id

        rid_token = request_id_var.set(request_id)
        cid_token = correlation_id_var.set(correlation_id)
        started = time.perf_counter()
        status_holder: dict[str, int] = {}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                mutable = MutableHeaders(scope=message)
                mutable[REQUEST_ID_HEADER] = request_id
                mutable[CORRELATION_ID_HEADER] = correlation_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            request_id_var.reset(rid_token)
            correlation_id_var.reset(cid_token)
            if self.access_log:
                log.info(
                    "http_request",
                    method=scope.get("method"),
                    path=scope.get("path"),
                    status=status_holder.get("status"),
                    duration_ms=round((time.perf_counter() - started) * 1000, 3),
                )


async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    """Never leak an internal error to a client; always leave a trace for us."""
    log.exception("unhandled_exception", path=request.url.path, error_type=type(exc).__name__)
    return JSONResponse({"error": "internal_error"}, status_code=500)


SECURITY_HEADERS: dict[str, str] = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "strict-transport-security": "max-age=31536000; includeSubDomains",
    "cross-origin-opener-policy": "same-origin",
}


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, extra: dict[str, str] | None = None) -> None:
        self.app = app
        self.headers = {**SECURITY_HEADERS, **(extra or {})}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                mutable = MutableHeaders(scope=message)
                for key, value in self.headers.items():
                    mutable[key] = value
            await send(message)

        await self.app(scope, receive, send_wrapper)


ProbeFn = Callable[[], Awaitable[None]]
