"""POST /v1/events — the ingest endpoint.

Everything here is shaped by one constraint: fixed, small work per request, with
no unbounded operation anywhere in the path. The endpoint authenticates, checks
size, validates, enqueues, and returns 202. It never queries Postgres, never
computes an aggregate, and never awaits Redis for the enqueue itself.
"""

from __future__ import annotations

import datetime as dt
import gzip
import uuid
import zlib

import msgspec
from mmp_core.ids import uuid7
from mmp_core.logging import get_logger
from mmp_crypto.pii import hash_ip
from mmp_ingest.schema import (
    MAX_EVENTS_PER_BATCH,
    EventBatch,
    ValidationFailure,
    validate_event,
)
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mmp_tracker.auth import AuthError
from mmp_tracker.state import TrackerState

log = get_logger(__name__)

_batch_decoder = msgspec.json.Decoder(EventBatch)


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("x-api-key")


async def _read_body(request: Request, state: TrackerState) -> bytes:
    """Read the body under a hard cap, decompressing safely if needed.

    Content-Length is a claim, not a fact, so the cap is enforced while reading
    rather than by trusting the header. Decompression is bounded separately: a
    few hundred kilobytes of gzip can expand to gigabytes, and an endpoint that
    decompresses whatever it is given has handed an unauthenticated caller a
    memory-exhaustion primitive.
    """
    limit = state.settings.max_payload_bytes
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ValidationFailure(f"payload exceeds {limit} bytes")
        chunks.append(chunk)
    body = b"".join(chunks)

    encoding = (request.headers.get("content-encoding") or "").lower()
    if encoding in ("gzip", "deflate"):
        max_decompressed = state.settings.max_decompressed_bytes
        try:
            decompressor = (
                zlib.decompressobj(zlib.MAX_WBITS | 16)
                if encoding == "gzip"
                else zlib.decompressobj()
            )
            # max_length caps the output; anything left in unconsumed_tail means
            # the payload was larger than we are willing to expand.
            body = decompressor.decompress(body, max_decompressed)
            if decompressor.unconsumed_tail:
                raise ValidationFailure(f"decompressed payload exceeds {max_decompressed} bytes")
        except (zlib.error, gzip.BadGzipFile) as exc:
            raise ValidationFailure("body is not valid compressed data") from exc
    return body


async def ingest_events(request: Request) -> Response:
    state: TrackerState = request.app.state.tracker
    started = dt.datetime.now(dt.UTC)

    presented = _bearer(request)
    if not presented:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        auth = await state.authenticator.authenticate(presented)
    except AuthError:
        # One message for every failure mode, so the response cannot be used to
        # distinguish an unknown key from a wrong secret.
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    allowed = await state.limiter.check(f"ingest:{auth.app_id}", state.ingest_limit)
    if not allowed.allowed:
        return JSONResponse(
            {"error": "rate_limited"},
            status_code=429,
            headers={"retry-after": str(max(1, allowed.retry_after_ms // 1000))},
        )

    try:
        body = await _read_body(request, state)
        batch = _batch_decoder.decode(body)
    except ValidationFailure as exc:
        return JSONResponse({"error": "invalid_payload", "detail": exc.reason}, status_code=413)
    except msgspec.DecodeError as exc:
        return JSONResponse({"error": "invalid_json", "detail": str(exc)}, status_code=400)

    if not batch.events:
        return JSONResponse({"accepted": 0, "duplicates": 0}, status_code=202)
    if len(batch.events) > MAX_EVENTS_PER_BATCH:
        return JSONResponse(
            {"error": "batch_too_large", "detail": f"at most {MAX_EVENTS_PER_BATCH} events"},
            status_code=413,
        )

    organization_id = uuid.UUID(auth.organization_id)
    app_id = uuid.UUID(auth.app_id)
    ip = _client_ip(request)
    ip_digest = hash_ip(ip, pepper=state.settings.ip_hash_pepper) if ip else None

    queued = []
    try:
        for index, incoming in enumerate(batch.events):
            event = validate_event(
                incoming,
                index=index,
                organization_id=organization_id,
                app_id=app_id,
                now=started,
                default_event_id=str(uuid7()),
            )
            # The raw address is used for the digest and then discarded; it is
            # never attached to the queued event or written anywhere.
            event.ip_hash = ip_digest
            queued.append(event)
    except ValidationFailure as exc:
        return JSONResponse({"error": "invalid_event", "detail": str(exc)}, status_code=422)

    # First idempotency layer: drop client retries before they reach the queue.
    event_ids = [event.event_id for event in queued]
    fresh = await state.idempotency.filter_new(auth.app_id, event_ids)
    accepted = [event for event in queued if event.event_id in fresh]
    duplicates = len(queued) - len(accepted)

    dropped = state.buffer.append(accepted)
    if dropped:
        # Un-mark what we could not enqueue, so the SDK's retry is not silently
        # swallowed by the idempotency window.
        await state.idempotency.forget(
            auth.app_id, [event.event_id for event in accepted[:dropped]]
        )

    state.accepted_total += len(accepted) - dropped
    return JSONResponse(
        {
            "accepted": len(accepted) - dropped,
            "duplicates": duplicates,
            "dropped": dropped,
        },
        status_code=202,
    )
