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
from mmp_core.metrics import events_accepted, observe_rejection
from mmp_crypto.pii import hash_device_id, hash_ip
from mmp_ingest.consent import ConsentSet, Mode, Purpose, State, minimise
from mmp_ingest.live import record_rejection
from mmp_ingest.schema import (
    MAX_EVENTS_PER_BATCH,
    EventBatch,
    QueuedEvent,
    ValidationFailure,
    canonical_event_name,
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
        observe_rejection("unauthorized")
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        auth = await state.authenticator.authenticate(presented)
    except AuthError:
        observe_rejection("unauthorized")
        # One message for every failure mode, so the response cannot be used to
        # distinguish an unknown key from a wrong secret.
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    allowed = await state.limiter.check(f"ingest:{auth.app_id}", state.ingest_limit)
    if not allowed.allowed:
        observe_rejection("rate_limited")
        await record_rejection(
            state.redis,
            auth.app_id,
            status=429,
            reason="rate_limited",
            detail="too many requests for this app; the SDK retries with backoff",
        )
        return JSONResponse(
            {"error": "rate_limited"},
            status_code=429,
            headers={"retry-after": str(max(1, allowed.retry_after_ms // 1000))},
        )

    try:
        body = await _read_body(request, state)
        batch = _batch_decoder.decode(body)
    except ValidationFailure as exc:
        observe_rejection("payload_too_large")
        await record_rejection(
            state.redis, auth.app_id, status=413, reason="payload_too_large", detail=exc.reason
        )
        return JSONResponse({"error": "invalid_payload", "detail": exc.reason}, status_code=413)
    except msgspec.DecodeError as exc:
        observe_rejection("invalid_json")
        await record_rejection(
            state.redis, auth.app_id, status=400, reason="invalid_json", detail=str(exc)
        )
        return JSONResponse({"error": "invalid_json", "detail": str(exc)}, status_code=400)

    if not batch.events:
        return JSONResponse({"accepted": 0, "duplicates": 0}, status_code=202)
    if len(batch.events) > MAX_EVENTS_PER_BATCH:
        observe_rejection("batch_too_large")
        await record_rejection(
            state.redis,
            auth.app_id,
            status=413,
            reason="batch_too_large",
            detail=f"at most {MAX_EVENTS_PER_BATCH} events per request",
            events_in_batch=len(batch.events),
        )
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
            _hash_advertising_ids(event, pepper=state.settings.ip_hash_pepper)
            queued.append(event)
    except ValidationFailure as exc:
        observe_rejection("invalid_event")
        # The whole batch is refused for one bad event, which is worth saying in
        # the live view: "3 events rejected" with no mention of the other two is
        # how someone concludes the SDK is losing data.
        await record_rejection(
            state.redis,
            auth.app_id,
            status=422,
            reason="invalid_event",
            detail=str(exc),
            events_in_batch=len(batch.events),
        )
        return JSONResponse({"error": "invalid_event", "detail": str(exc)}, status_code=422)

    # Consent, before anything is queued.
    #
    # Checked here rather than downstream because consent applied after
    # persistence is a deletion problem: the data is already in a partition, a
    # rollup, a postback and a partner's system. Applied at the edge, a denied
    # purpose means the field never existed.
    await _apply_consent(state, auth.app_id, queued, mode=auth.consent_mode)

    # Sessions are decided server-side, after validation. The SDK reports
    # activity; where the boundaries fall is ours to say, or two devices with
    # different clock behaviour would produce incomparable session counts.
    await _assign_sessions(state, auth.app_id, queued)

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

    written = len(accepted) - dropped
    state.accepted_total += written
    state.accepted_counter.record(auth.app_id, written)
    events_accepted.labels(source="sdk").inc(written)
    if dropped:
        observe_rejection("shed")
    return JSONResponse(
        {
            "accepted": len(accepted) - dropped,
            "duplicates": duplicates,
            "dropped": dropped,
        },
        status_code=202,
    )


# Advertising IDs are hashed here, at the edge, and the raw values removed from
# the payload before it is queued. Attribution needs to compare the identifier
# seen at click time with the one seen at install time, which a digest supports;
# it never needs the identifier itself. Doing this at ingest rather than in the
# worker means the raw value exists only in this process, for the length of one
# request, and is never written to the queue, the log, or the database.
ADVERTISING_ID_KEYS = ("gaid", "idfa", "advertising_id", "device_id")


def _hash_advertising_ids(event: QueuedEvent, *, pepper: str) -> None:
    properties = event.properties
    if not properties:
        return
    raw = next((properties[key] for key in ADVERTISING_ID_KEYS if properties.get(key)), None)
    for key in ADVERTISING_ID_KEYS:
        properties.pop(key, None)
    if not isinstance(raw, str):
        return
    digest = hash_device_id(raw, pepper=pepper)
    if digest is not None:
        properties["device_hash"] = digest.hex()


# Session assignment.
#
# Runs after validation so an invalid batch never touches session state, and
# before queueing so every event carries its session id downstream. One Redis
# round trip per distinct device in the batch — a batch is usually one device,
# so usually one round trip.
async def _assign_sessions(state: TrackerState, app_id: str, events: list[QueuedEvent]) -> None:
    """Attach a session id to every event that lacks one.

    Deduplicated by device and resolved in one pipelined round trip. Doing this
    per event cost a Redis hop each and nearly tripled ingest p50 on a
    twenty-event batch — caught by the latency gate rather than in review.
    """
    needing: dict[str, str] = {}
    ends: list[QueuedEvent] = []

    for event in events:
        if event.session_id:
            # The SDK supplied one. Honoured, because a client that tracks its
            # own foreground and background transitions knows about boundaries
            # the server cannot see.
            continue
        if event.event_name == "session_end":
            ends.append(event)
            continue
        # First event for this device in the batch decides its session; the
        # rest join it, which is what belonging to one session means.
        needing.setdefault(event.anonymous_id, event.event_name)

    if needing:
        decisions = await state.sessions.resolve_many(app_id=app_id, devices=list(needing.items()))
        for event in events:
            if event.session_id or event.event_name == "session_end":
                continue
            decision = decisions.get(event.anonymous_id)
            if decision is not None:
                event.session_id = str(decision.session_id)

    for event in ends:
        ended = await state.sessions.end(app_id=app_id, anonymous_id=event.anonymous_id)
        if ended is not None:
            event.session_id = str(ended)


# The event an SDK sends to report a consent decision. Handled as a normal event
# so it travels the same authenticated, rate-limited, validated path as anything
# else, rather than needing an endpoint of its own with its own auth.
# Matched canonically, so "consent_update", "Consent-Update" and "consentUpdate"
# all record the user's decision rather than becoming an ordinary event that
# quietly fails to apply it.
CONSENT_EVENT = "consentupdate"


async def _apply_consent(
    state: TrackerState,
    app_id: str,
    events: list[QueuedEvent],
    *,
    mode: str = "permissive",
) -> None:
    """Record consent updates, then minimise everything else accordingly.

    Consent events are processed first and in order, so a batch that reports a
    grant and then sends data under it behaves as the SDK intended — an SDK
    flushing after the user accepted a dialogue sends exactly that batch.
    """
    for event in events:
        if canonical_event_name(event.event_name) != CONSENT_EVENT:
            continue
        states: dict[Purpose, State] = {}
        for key, value in (event.properties or {}).items():
            try:
                purpose = Purpose(key)
                states[purpose] = State(str(value))
            except ValueError:
                # An unknown purpose or state is ignored rather than rejected:
                # an older SDK reporting a purpose we have since renamed should
                # not lose its whole batch. Debug rather than warning — at
                # ingest volume one line per stale SDK is its own incident.
                log.debug("consent_purpose_unrecognised", purpose=key)
                continue
        if states:
            await state.consent.record(app_id, event.anonymous_id, states)

    # One lookup per distinct device, not per event.
    devices = {event.anonymous_id for event in events}
    consents = await state.consent.lookup_many(app_id, list(devices), mode=Mode(mode))

    for event in events:
        consent = consents.get(event.anonymous_id, ConsentSet())
        event.properties = minimise(event.properties, consent)
        if not consent.allows(Purpose.ATTRIBUTION):
            # Not merely stripped from properties: these are first-class columns
            # and would otherwise carry the identifier past the minimiser.
            event.click_id = None
            event.ip_hash = None
