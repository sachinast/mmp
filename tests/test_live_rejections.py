"""Rejections kept for the live view.

A rejected request is the failure an integrator most needs to see and the one
nothing used to record: the SDK got a 4xx, a metric moved, and the dashboard
showed nothing at all.
"""

from __future__ import annotations

import datetime as dt

import msgspec
from mmp_ingest.live import (
    REJECTIONS_MAX,
    recent_rejections,
    record_rejection,
    rejections_key,
)

from tests.conftest_ingest import sample_event, signed_headers

AN_HOUR_AGO = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)


def _sdk_headers(seeded_app) -> dict[str, str]:
    return {"authorization": f"Bearer {seeded_app['api_key']}"}


async def _rejections(redis, seeded_app):
    return await recent_rejections(redis, str(seeded_app["app_id"]), since=AN_HOUR_AGO)


async def test_an_invalid_event_is_recorded_with_its_reason(tracker, seeded_app, ingest_redis):
    response = await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(event_name="purchase", anonymous_id="d1"),
                # Revenue without a currency: refused, and it takes the whole
                # batch with it.
                sample_event(event_name="purchase", anonymous_id="d2", revenue_minor=499),
            ]
        },
        headers=_sdk_headers(seeded_app),
    )
    assert response.status_code == 422

    entries = await _rejections(ingest_redis, seeded_app)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["reason"] == "invalid_event"
    assert entry["status"] == 422
    assert entry["events_in_batch"] == 2, "the live view should say the whole batch went"
    assert entry["source"] == "sdk"
    assert "currency" in entry["detail"]


async def test_an_accepted_event_records_nothing(tracker, seeded_app, ingest_redis):
    """Written only on the failure path; the success path pays nothing."""
    response = await tracker.post(
        "/v1/events",
        json={"events": [sample_event(event_name="app_open", anonymous_id="ok")]},
        headers=_sdk_headers(seeded_app),
    )
    assert response.status_code == 202
    assert await _rejections(ingest_redis, seeded_app) == []


async def test_malformed_json_is_recorded(tracker, seeded_app, ingest_redis):
    response = await tracker.post(
        "/v1/events",
        content=b'{"events": [',
        headers={**_sdk_headers(seeded_app), "content-type": "application/json"},
    )
    assert response.status_code == 400
    assert [e["reason"] for e in await _rejections(ingest_redis, seeded_app)] == ["invalid_json"]


async def test_the_payload_itself_is_never_kept(tracker, seeded_app, ingest_redis):
    """The body of a rejected request is exactly the data that failed validation —
    possibly personal data sent by mistake. Only our message about it is kept."""
    marker = "someone@example.com-sent-by-mistake"
    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="purchase",
                    anonymous_id="d3",
                    revenue_minor=100,
                    properties={"email": marker},
                )
            ]
        },
        headers=_sdk_headers(seeded_app),
    )
    raw = await ingest_redis.lrange(rejections_key(str(seeded_app["app_id"])), 0, -1)
    assert raw, "the rejection should have been recorded"
    assert all(marker.encode() not in item for item in raw)


async def test_rate_limiting_is_noted_once_per_window(ingest_redis):
    """A client being rate limited is sending a lot of requests. Recording every
    one would turn an abusive burst into a Redis write storm."""
    app_id = "rate-limited-app"
    await ingest_redis.delete(rejections_key(app_id), f"{rejections_key(app_id)}:rate-limit-noted")
    for _ in range(25):
        await record_rejection(ingest_redis, app_id, status=429, reason="rate_limited")
    entries = await recent_rejections(ingest_redis, app_id, since=AN_HOUR_AGO)
    assert len(entries) == 1


async def test_the_buffer_is_capped(ingest_redis):
    app_id = "noisy-app"
    await ingest_redis.delete(rejections_key(app_id))
    for index in range(REJECTIONS_MAX + 30):
        await record_rejection(
            ingest_redis, app_id, status=422, reason="invalid_event", detail=str(index)
        )
    assert await ingest_redis.llen(rejections_key(app_id)) == REJECTIONS_MAX
    ttl = await ingest_redis.ttl(rejections_key(app_id))
    assert 0 < ttl <= 3600, "an app that stops sending should cost nothing an hour later"
    newest = await recent_rejections(ingest_redis, app_id, since=AN_HOUR_AGO)
    assert newest[0]["detail"] == str(REJECTIONS_MAX + 29), "newest first"


async def test_a_redis_failure_never_changes_the_response(tracker, seeded_app, monkeypatch):
    """Recording a rejection must not turn a 422 into a 500."""
    import mmp_ingest.live as live

    class Broken:
        def pipeline(self, **_kwargs):
            raise ConnectionError("redis is down")

        async def set(self, *_args, **_kwargs):
            raise ConnectionError("redis is down")

    original = live.record_rejection

    async def with_broken_redis(_redis, *args, **kwargs):
        return await original(Broken(), *args, **kwargs)

    monkeypatch.setattr("mmp_tracker.ingest.record_rejection", with_broken_redis)

    response = await tracker.post(
        "/v1/events",
        json={"events": [sample_event(event_name="purchase", anonymous_id="d4", revenue_minor=1)]},
        headers=_sdk_headers(seeded_app),
    )
    assert response.status_code == 422


async def test_an_sdk_key_on_the_s2s_endpoint_says_why(tracker, seeded_app, ingest_redis):
    """The most common server-to-server mistake. The caller still gets a bare
    401; the reason goes to the live view."""
    body = msgspec.json.encode(
        {
            "events": [
                {
                    "event_name": "purchase",
                    "anonymous_id": "s",
                    "revenue_minor": 1,
                    "currency": "USD",
                }
            ]
        }
    )
    response = await tracker.post(
        "/v1/s2s/events",
        content=body,
        headers={**_sdk_headers(seeded_app), "content-type": "application/json"},
    )
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}, "the response itself reveals nothing"

    entries = await _rejections(ingest_redis, seeded_app)
    assert entries[0]["reason"] == "sdk_key_on_s2s"
    assert entries[0]["source"] == "s2s"


async def test_a_bad_signature_is_recorded_without_hinting_at_the_right_one(
    tracker, seeded_app, s2s_key, ingest_redis
):
    body = msgspec.json.encode(
        {
            "events": [
                {
                    "event_name": "purchase",
                    "anonymous_id": "s",
                    "revenue_minor": 1,
                    "currency": "USD",
                }
            ]
        }
    )
    headers = signed_headers(s2s_key["api_key"], s2s_key["secret"], body)
    presented = "v1=" + "0" * 64
    headers["x-mmp-signature"] = presented
    response = await tracker.post("/v1/s2s/events", content=body, headers=headers)
    assert response.status_code == 401

    entries = await _rejections(ingest_redis, seeded_app)
    assert entries[0]["reason"] == "invalid_signature"
    assert "canonical string" in entries[0]["detail"]
    assert presented not in entries[0]["detail"]
    assert s2s_key["secret"] not in entries[0]["detail"]
