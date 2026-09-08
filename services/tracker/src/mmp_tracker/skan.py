"""POST /.well-known/skadnetwork/report-attribution — Apple's postbacks.

The only unauthenticated write path in the platform. Apple's devices post here
with no credential of any kind, so the ECDSA signature is the entire
authentication and this handler is written on the assumption that most of what
reaches it is not from Apple.

The path is Apple's convention for the developer copy of a postback; an ad
network configures its own URL at registration, and either can point here.

Four decisions worth stating, because each one is a place this could go wrong:

**Verify before anything else.** No database lookup, no logging of contents, no
parsing beyond JSON, until the signature is Apple's. Otherwise the endpoint
becomes a way to make us do work — and to write attacker-chosen strings into our
logs — for free.

**Answer 200 to anything genuinely from Apple.** Apple retries for up to nine
days when the receiver does not answer 200. A postback that is validly signed
but that we cannot use — an app we do not know, one we have already stored — is
still *answered*, because the alternative is nine days of retries that will fail
identically. Only unverifiable requests get a 4xx, and those are not from Apple.

**The response says nothing.** Same body for stored, duplicate, and unknown-app.
A caller learning which app ids we know would be able to enumerate our customer
list from an endpoint that requires no credential.

**Duplicates are ordinary.** The retry behaviour above means the same postback
arrives repeatedly by design. The unique constraint on ``transaction_id`` turns
that into a no-op rather than a duplicate install.
"""

from __future__ import annotations

import json
from typing import Any

from asyncpg.exceptions import UniqueViolationError
from mmp_attrib.skadnetwork import Rejected, Verified, verify
from mmp_core.ids import uuid7
from mmp_core.logging import get_logger
from mmp_core.metrics import skan_postbacks
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mmp_tracker.state import TrackerState

log = get_logger(__name__)

# Apple's postbacks are small and fixed in shape. Anything larger is not one,
# and reading it would be doing an anonymous caller's work for them.
MAX_BODY_BYTES = 8 * 1024

RESOLVE_APP_SQL = """
SELECT id, organization_id
FROM apps
WHERE apple_app_id = $1 AND status = 'active'
"""

INSERT_SQL = """
INSERT INTO skadnetwork_postbacks (
    id, organization_id, app_id, version, ad_network_id, apple_app_id,
    transaction_id, source_identifier, did_win, redownload, fidelity_type,
    conversion_value, coarse_value, postback_sequence_index,
    source_app_id, source_domain, payload, received_at
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
        $17::jsonb, now())
"""

# A plain INSERT, with the duplicate caught rather than avoided.
#
# Both obvious alternatives need SELECT on the table, which the tracker does not
# have and should not: RETURNING reads the row back, and so — less obviously —
# does ON CONFLICT with a conflict target, because PostgreSQL has to inspect the
# arbiter index. Dropping the target instead would work, but it would also
# silently swallow any *other* unique violation, and a constraint failing that
# we did not anticipate is something to hear about rather than discard.
DUPLICATE_CONSTRAINT = "uq_skadnetwork_postbacks_transaction"

# One body, whatever happened. See the module docstring.
ACCEPTED: dict[str, str] = {"status": "ok"}


async def receive_postback(request: Request) -> Response:
    state: TrackerState = request.app.state.tracker

    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        skan_postbacks.labels(outcome="oversized").inc()
        return JSONResponse({"error": "payload too large"}, status_code=413)

    try:
        payload: Any = json.loads(body)
    except ValueError:
        skan_postbacks.labels(outcome="malformed").inc()
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(payload, dict):
        skan_postbacks.labels(outcome="malformed").inc()
        return JSONResponse({"error": "invalid json"}, status_code=400)

    result = verify(payload)
    if isinstance(result, Rejected):
        # The reason is recorded, never the payload: this is attacker-controlled
        # content and writing it to the log would make our logs a place someone
        # else can put arbitrary strings.
        skan_postbacks.labels(outcome="rejected").inc()
        log.warning("skan_postback_rejected", reason=str(result.reason))
        return JSONResponse({"error": "signature verification failed"}, status_code=400)

    await _store(state, result, body)
    return JSONResponse(ACCEPTED, status_code=200)


async def _store(state: TrackerState, postback: Verified, raw: bytes) -> None:
    """Resolve the app and record the postback.

    Failures here are logged and swallowed rather than returned. Apple has
    already been told 200, and correctly so: a storage problem on our side is
    not something nine days of retries would fix, and refusing the response
    would leave the device retrying a postback we do have.
    """
    try:
        apple_app_id = int(postback.app_id)
    except ValueError:
        skan_postbacks.labels(outcome="unknown_app").inc()
        return

    async with state.database.acquire_raw() as conn:
        app = await conn.fetchrow(RESOLVE_APP_SQL, apple_app_id)
        if app is None:
            # A postback for an app that is not ours, or one whose App Store id
            # nobody has configured. Counted rather than logged per request:
            # an unregistered id is exactly what a flood would use, and one log
            # line each would make that a way to fill our disk.
            skan_postbacks.labels(outcome="unknown_app").inc()
            return

        try:
            await conn.execute(
                INSERT_SQL,
                uuid7(),
                app["organization_id"],
                app["id"],
                postback.version,
                postback.ad_network_id,
                apple_app_id,
                postback.transaction_id,
                postback.source_identifier,
                postback.did_win,
                postback.redownload,
                postback.fidelity_type,
                postback.conversion_value,
                postback.coarse_value,
                postback.postback_sequence_index,
                postback.source_app_id,
                postback.source_domain,
                raw.decode("utf-8", errors="replace"),
            )
        except UniqueViolationError as exc:
            if exc.constraint_name != DUPLICATE_CONSTRAINT:
                raise
            # Expected, not exceptional: Apple retries until it gets a 200, so
            # the same postback legitimately arrives more than once.
            skan_postbacks.labels(outcome="duplicate").inc()
            return

    skan_postbacks.labels(outcome="stored" if postback.did_win else "stored_nonwinner").inc()
