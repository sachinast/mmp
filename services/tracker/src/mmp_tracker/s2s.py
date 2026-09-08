"""POST /v1/s2s/events — server-to-server conversions.

An advertiser's backend posts here directly: purchases confirmed after payment
clears, refunds, subscription renewals. Events that no device can be trusted to
report, because the device is where the fraud is.

Two differences from the SDK endpoint, both deliberate:

* **A separate credential class.** An SDK key ships inside an app and must be
  assumed public — anyone can pull it out of an APK. An S2S key lives on a
  server and can be trusted with more. Using one for both would mean either
  trusting a public value or crippling the server integration.
* **Request signing with replay protection.** A captured S2S request replayed is
  a duplicate conversion sent to an ad network, which is money. Signature plus
  timestamp plus nonce closes that; a bearer token alone does not.

Everything downstream is identical. The same stream, the same worker, the same
table — a separate storage path for S2S events would mean two sets of numbers
that have to be reconciled, and they never quite are.
"""

from __future__ import annotations

import datetime as dt
import uuid

import msgspec
from mmp_core.ids import uuid7
from mmp_core.logging import get_logger
from mmp_core.metrics import events_accepted, observe_rejection
from mmp_crypto.signing import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    SignatureError,
    nonce_for,
    verify,
)
from mmp_ingest.schema import (
    MAX_EVENTS_PER_BATCH,
    EventBatch,
    ValidationFailure,
    validate_event,
)
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mmp_tracker.auth import AuthError
from mmp_tracker.ingest import _read_body
from mmp_tracker.state import TrackerState

log = get_logger(__name__)

_batch_decoder = msgspec.json.Decoder(EventBatch)

# Long enough to cover the signature's own timestamp tolerance with margin. A
# nonce that expired before the timestamp did would leave a window in which a
# captured request could be replayed after its cache entry aged out.
REPLAY_WINDOW = dt.timedelta(minutes=15)

# Events a server may report. An SDK-only event arriving over S2S means either a
# misconfigured integration or someone fabricating engagement, and neither
# should be quietly accepted.
S2S_EVENTS = frozenset(
    {
        "purchase",
        "refund",
        "subscription_start",
        "subscription_renew",
        "subscription_cancel",
        "signup",
        "login",
    }
)


async def ingest_s2s(request: Request) -> Response:
    state: TrackerState = request.app.state.tracker
    started = dt.datetime.now(dt.UTC)

    presented = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not presented:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        auth = await state.authenticator.authenticate(presented)
    except AuthError:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if auth.kind != "s2s":
        # An SDK key shipped inside an app is public by construction. Accepting
        # one here would give anyone who unpacked the APK the ability to
        # fabricate purchases.
        log.warning("s2s_rejected_sdk_key", app_id=auth.app_id)
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await _read_body(request, state)
    except ValidationFailure as exc:
        return JSONResponse({"error": "invalid_payload", "detail": exc.reason}, status_code=413)

    signature = request.headers.get(SIGNATURE_HEADER)
    timestamp = request.headers.get(TIMESTAMP_HEADER)
    if not signature or not timestamp:
        return JSONResponse(
            {
                "error": "signature_required",
                "detail": f"{SIGNATURE_HEADER} and {TIMESTAMP_HEADER} are required",
            },
            status_code=401,
        )

    secret = await state.s2s_secret_for(auth)
    if secret is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        verify(
            method=request.method,
            path=request.url.path,
            body=body,
            secret=secret,
            signature=signature,
            timestamp=timestamp,
            now=started,
        )
    except SignatureError:
        return JSONResponse({"error": "invalid_signature"}, status_code=401)

    # Signature verified — now make sure this exact request has not been seen.
    # The signature is unique per (secret, method, path, timestamp, body), which
    # is precisely what a replay would have to reproduce.
    fresh = await state.redis.set(
        f"s2s:nonce:{auth.app_id}:{nonce_for(signature=signature)}",
        1,
        ex=int(REPLAY_WINDOW.total_seconds()),
        nx=True,
    )
    if not fresh:
        log.warning("s2s_replay_rejected", app_id=auth.app_id)
        observe_rejection("replayed")
        return JSONResponse({"error": "replayed_request"}, status_code=409)

    try:
        batch = _batch_decoder.decode(body)
    except msgspec.DecodeError as exc:
        return JSONResponse({"error": "invalid_json", "detail": str(exc)}, status_code=400)

    if not batch.events:
        return JSONResponse({"accepted": 0, "duplicates": 0}, status_code=202)
    if len(batch.events) > MAX_EVENTS_PER_BATCH:
        return JSONResponse({"error": "batch_too_large"}, status_code=413)

    organization_id = uuid.UUID(auth.organization_id)
    app_id = uuid.UUID(auth.app_id)

    queued = []
    try:
        for index, incoming in enumerate(batch.events):
            if incoming.event_name not in S2S_EVENTS:
                raise ValidationFailure(
                    f"{incoming.event_name!r} cannot be reported over S2S", index=index
                )
            event = validate_event(
                incoming,
                index=index,
                organization_id=organization_id,
                app_id=app_id,
                now=started,
                default_event_id=str(uuid7()),
            )
            # A server has no device address worth recording, and inventing one
            # from the calling server's IP would attribute every conversion to a
            # data centre.
            event.ip_hash = None
            queued.append(event)
    except ValidationFailure as exc:
        return JSONResponse({"error": "invalid_event", "detail": str(exc)}, status_code=422)

    event_ids = [event.event_id for event in queued]
    new_ids = await state.idempotency.filter_new(auth.app_id, event_ids)
    accepted = [event for event in queued if event.event_id in new_ids]
    duplicates = len(queued) - len(accepted)

    dropped = state.buffer.append(accepted)
    if dropped:
        await state.idempotency.forget(
            auth.app_id, [event.event_id for event in accepted[:dropped]]
        )

    written = len(accepted) - dropped
    state.accepted_total += written
    state.accepted_counter.record(auth.app_id, written)
    events_accepted.labels(source="s2s").inc(written)
    return JSONResponse(
        {"accepted": len(accepted) - dropped, "duplicates": duplicates, "dropped": dropped},
        status_code=202,
    )
