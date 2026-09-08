"""Server-to-server conversions: signed, fresh, and not replayable.

A captured S2S request replayed is a duplicate conversion sent to an ad network,
which is money. A bearer token alone does not stop that.
"""

from __future__ import annotations

import datetime as dt

import msgspec
import pytest

from tests.conftest_ingest import flush_tracker, signed_headers


def payload(**overrides) -> bytes:
    event = {
        "event_id": None,
        "event_name": "purchase",
        "anonymous_id": "s2s-device",
        "revenue_minor": 4999,
        "currency": "USD",
    }
    event.update(overrides)
    event = {k: v for k, v in event.items() if v is not None}
    return msgspec.json.encode({"events": [event]})


async def test_signed_request_is_accepted(tracker, s2s_key):
    body = payload()
    response = await tracker.post(
        "/v1/s2s/events",
        content=body,
        headers=signed_headers(s2s_key["api_key"], s2s_key["secret"], body),
    )
    assert response.status_code == 202, response.text
    assert response.json()["accepted"] == 1


async def test_unsigned_request_is_rejected(tracker, s2s_key):
    body = payload()
    response = await tracker.post(
        "/v1/s2s/events",
        content=body,
        headers={
            "authorization": f"Bearer {s2s_key['api_key']}",
            "content-type": "application/json",
        },
    )
    assert response.status_code == 401
    assert response.json()["error"] == "signature_required"


async def test_tampered_body_is_rejected(tracker, s2s_key):
    """The signature covers a digest of the body, so changing the amount after
    signing invalidates it."""
    body = payload(revenue_minor=4999)
    headers = signed_headers(s2s_key["api_key"], s2s_key["secret"], body)
    tampered = payload(revenue_minor=999999)

    response = await tracker.post("/v1/s2s/events", content=tampered, headers=headers)
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_signature"


async def test_signature_from_another_endpoint_is_rejected(tracker, s2s_key):
    """The path is in the signed string, so a signature captured for one
    endpoint cannot be replayed against another."""
    body = payload()
    headers = signed_headers(s2s_key["api_key"], s2s_key["secret"], body, path="/v1/events")
    response = await tracker.post("/v1/s2s/events", content=body, headers=headers)
    assert response.status_code == 401


async def test_stale_timestamp_is_rejected(tracker, s2s_key):
    from mmp_crypto.signing import sign

    body = payload()
    old = sign(
        method="POST",
        path="/v1/s2s/events",
        body=body,
        secret=s2s_key["secret"],
        now=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2),
    )
    response = await tracker.post(
        "/v1/s2s/events",
        content=body,
        headers={"authorization": f"Bearer {s2s_key['api_key']}", **old.headers()},
    )
    assert response.status_code == 401


async def test_replayed_request_is_rejected(tracker, s2s_key):
    """The whole reason signing is not enough on its own.

    The identical request, headers and all — which is exactly what an attacker
    who captured one would send.
    """
    body = payload()
    headers = signed_headers(s2s_key["api_key"], s2s_key["secret"], body)

    first = await tracker.post("/v1/s2s/events", content=body, headers=headers)
    second = await tracker.post("/v1/s2s/events", content=body, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 409, "a replayed request must be refused"
    assert second.json()["error"] == "replayed_request"


async def test_an_sdk_key_cannot_be_used_for_s2s(tracker, seeded_app, s2s_key):
    """An SDK key ships inside an app and is public by construction.

    Accepting one here would let anyone who unpacked the APK fabricate
    purchases.
    """
    body = payload()
    headers = signed_headers(seeded_app["api_key"], s2s_key["secret"], body)
    response = await tracker.post("/v1/s2s/events", content=body, headers=headers)
    assert response.status_code == 401


@pytest.mark.parametrize("event_name", ["install", "app_open", "session_start"])
async def test_device_only_events_are_refused_over_s2s(tracker, s2s_key, event_name):
    """An SDK-only event arriving over S2S is either a misconfigured integration
    or someone fabricating engagement."""
    body = payload(event_name=event_name)
    response = await tracker.post(
        "/v1/s2s/events",
        content=body,
        headers=signed_headers(s2s_key["api_key"], s2s_key["secret"], body),
    )
    assert response.status_code == 422


async def test_s2s_events_share_the_normal_pipeline(
    tracker, worker_consumer, owner_conn, seeded_app, s2s_key
):
    """One storage path, or there are two sets of numbers to reconcile — and
    they never quite reconcile."""
    body = payload()
    await tracker.post(
        "/v1/s2s/events",
        content=body,
        headers=signed_headers(s2s_key["api_key"], s2s_key["secret"], body),
    )
    await flush_tracker(tracker)
    for _ in range(3):
        if await worker_consumer.run_once() == 0:
            break

    row = await owner_conn.fetchrow(
        "SELECT event_name, revenue_minor, ip_hash FROM events "
        "WHERE app_id = $1 AND anonymous_id = 's2s-device'",
        seeded_app["app_id"],
    )
    assert row is not None, "an S2S event must land in the same events table"
    assert row["revenue_minor"] == 4999
    # A server has no device address worth recording, and inventing one from the
    # calling server's IP would attribute every conversion to a data centre.
    assert row["ip_hash"] is None
