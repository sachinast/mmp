"""POST /v1/deeplink/resolve — the deferred deep link handshake.

Someone taps a link for one product, does not have the app, installs it, and
opens it. The app asks here what the person was originally after, and lands them
there instead of on a home screen.

This is the one place the destination crosses back to the device, so the
constraints are about disclosure rather than latency. It runs once per install,
not once per click, so a single indexed query is affordable — but it answers
questions about a device, to a caller holding a key that ships inside a public
app binary, and both of those shape what it will say.

**It is bounded in time.** A destination resolves only inside a short window
after the install. That matches the product — deferred deep linking is a
first-launch concern — and it means the endpoint cannot be walked backwards
through an app's install history months later.

**It does not confirm or deny.** A device with no attribution, a device outside
the window, and a device that was attributed to a link carrying no destination
all produce the same answer. Otherwise this becomes an oracle for whether a
given identifier installed a given app, which is exactly the cross-app
correlation the rest of this system is built to avoid.

**It never returns anything the advertiser did not put there.** The value came
from the click, where it was validated by ``mmp_core.deeplinks`` before storage.
Nothing here re-derives it or accepts one from the request.
"""

from __future__ import annotations

import msgspec
from mmp_core.logging import get_logger
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mmp_tracker.auth import AuthError
from mmp_tracker.state import TrackerState

log = get_logger(__name__)

# A first launch happens within seconds of the install; a day is generous cover
# for a person who installs, does not open, and comes back tomorrow. Beyond that
# the original intent is stale anyway — sending someone to a product they looked
# at last week is worse than sending them to the home screen.
RESOLVE_WINDOW_HOURS = 24

MAX_ANONYMOUS_ID = 128

# Scoped to the app and to a live attribution, so this can never read across
# tenants even if the app id were wrong — the API key decides the app id, and
# the row must match it.
RESOLVE_SQL = """
SELECT deep_link
FROM attributions
WHERE app_id = $1
  AND install_key = $2
  AND superseded_by IS NULL
  AND installed_at >= now() - ($3 || ' hours')::interval
"""


class ResolveRequest(msgspec.Struct):
    anonymous_id: str


_decoder = msgspec.json.Decoder(ResolveRequest)

# One shape for every outcome. See the module docstring: a distinguishable
# "not found" would turn this into an install oracle.
NOT_FOUND = {"destination": None, "matched": False}


async def resolve_deferred(request: Request) -> Response:
    state: TrackerState = request.app.state.tracker

    header = request.headers.get("authorization")
    presented = header[7:].strip() if header and header.lower().startswith("bearer ") else None
    if not presented:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        auth = await state.authenticator.authenticate(presented)
    except AuthError:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # Rate limited on its own budget rather than sharing the ingest one: this
    # endpoint takes a database query, and it is the one an attacker would use
    # to probe identifiers.
    allowed = await state.limiter.check(f"deeplink:{auth.app_id}", state.ingest_limit)
    if not allowed.allowed:
        return JSONResponse(
            {"error": "rate_limited"},
            status_code=429,
            headers={"retry-after": str(max(1, allowed.retry_after_ms // 1000))},
        )

    body = await request.body()
    if len(body) > 4096:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    try:
        parsed = _decoder.decode(body)
    except msgspec.DecodeError:
        return JSONResponse({"error": "invalid request"}, status_code=400)

    anonymous_id = parsed.anonymous_id.strip()
    if not anonymous_id or len(anonymous_id) > MAX_ANONYMOUS_ID:
        return JSONResponse({"error": "invalid request"}, status_code=400)

    async with state.database.acquire_raw() as conn:
        destination = await conn.fetchval(
            RESOLVE_SQL,
            auth.app_id,
            f"{auth.app_id}:{anonymous_id}",
            str(RESOLVE_WINDOW_HOURS),
        )

    if not destination:
        return JSONResponse(NOT_FOUND, headers={"cache-control": "no-store"})

    return JSONResponse(
        {"destination": destination, "matched": True},
        headers={"cache-control": "no-store"},
    )
