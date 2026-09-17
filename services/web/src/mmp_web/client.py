"""The dashboard's client for the business API.

The web service holds no database connection and no business logic. It renders
what the API returns, and it forwards the user's own session cookie so the API
performs authorisation exactly as it would for any other caller.

That is the point of the separation: the dashboard cannot see anything a user
could not see through the API, because it *is* the user as far as the API is
concerned. There is no service account here to leak, and no path by which a
template bug becomes a tenancy bug.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from mmp_core.context import CORRELATION_ID_HEADER, correlation_id_var
from mmp_core.logging import get_logger

log = get_logger(__name__)

SESSION_COOKIE = "mmp_session"
CSRF_COOKIE = "mmp_csrf"
CSRF_HEADER = "x-csrf-token"

# A dashboard page that hangs is worse than one that fails: the user waits,
# retries, and doubles the load on an API that is already struggling.
DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=2.0)


class ApiError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{status_code}: {detail}")


class Unauthorized(ApiError):
    """The session is gone or was never valid. Always a redirect to login."""


@dataclass
class ApiClient:
    base_url: str
    session_token: str | None = None
    csrf_token: str | None = None

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.csrf_token:
            headers[CSRF_HEADER] = self.csrf_token
        # Carried through so a slow dashboard page can be traced to the API
        # request that made it slow, across two services.
        correlation_id = correlation_id_var.get()
        if correlation_id:
            headers[CORRELATION_ID_HEADER] = correlation_id
        return headers

    def _cookies(self) -> dict[str, str]:
        return {SESSION_COOKIE: self.session_token} if self.session_token else {}

    async def request(self, method: str, path: str, **kwargs: Any) -> tuple[Any, httpx.Headers]:
        # Cookies go on the client, not the request. httpx deprecated the
        # per-request form because persistence across a redirect is ambiguous —
        # and a session cookie that silently failed to travel would look exactly
        # like an expired session, which is a miserable thing to debug.
        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=DEFAULT_TIMEOUT, cookies=self._cookies()
        ) as client:
            response = await client.request(method, path, headers=self._headers(), **kwargs)

        if response.status_code == 401:
            raise Unauthorized(401, "not authenticated")
        if response.status_code >= 400:
            detail = "request failed"
            try:
                body = response.json()
                detail = body.get("detail") or body.get("error") or detail
            except ValueError:
                # A non-JSON error body (a proxy's HTML error page, say) tells
                # the user nothing useful; the generic message is better.
                log.debug("api_error_body_not_json", status=response.status_code)
            raise ApiError(response.status_code, str(detail))

        if response.status_code == 204 or not response.content:
            return None, response.headers
        return response.json(), response.headers

    async def get(self, path: str, **kwargs: Any) -> Any:
        data, _headers = await self.request("GET", path, **kwargs)
        return data

    async def get_with_headers(self, path: str, **kwargs: Any) -> tuple[Any, httpx.Headers]:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> Any:
        data, _headers = await self.request("POST", path, **kwargs)
        return data

    @asynccontextmanager
    async def stream(self, method: str, path: str, **kwargs: Any) -> AsyncIterator[httpx.Response]:
        """Stream a response through, rather than buffering it.

        Used for exports, which can be a million rows. Reading one into memory
        to hand it on would make a single download decide how much RAM the
        dashboard needs — the same reason the API streams it in the first place.

        The timeout is deliberately not the dashboard's usual one: a page that
        hangs for ten seconds is a bad page, but an export legitimately takes
        longer than any page should.
        """
        async with (
            httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(300.0, connect=5.0),
                cookies=self._cookies(),
            ) as client,
            client.stream(method, path, headers=self._headers(), **kwargs) as response,
        ):
            if response.status_code == 401:
                raise Unauthorized(401, "not authenticated")
            if response.status_code >= 400:
                await response.aread()
                raise ApiError(response.status_code, "export failed")
            yield response

    async def patch(self, path: str, **kwargs: Any) -> Any:
        data, _headers = await self.request("PATCH", path, **kwargs)
        return data

    async def delete(self, path: str, **kwargs: Any) -> Any:
        data, _headers = await self.request("DELETE", path, **kwargs)
        return data
