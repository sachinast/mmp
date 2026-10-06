"""Blocked events are dropped at the edge, before anything is queued."""

from __future__ import annotations

from mmp_core.ids import uuid7
from mmp_ingest.catalogue import blocked_events_cache_key

from tests.conftest_ingest import sample_event


async def _define(owner_conn, seeded_app, name: str, *, status: str) -> None:
    await owner_conn.execute(
        """INSERT INTO event_definitions
               (id, organization_id, app_id, name, display_name, kind, status)
           VALUES ($1, $2, $3, $4, $4, 'custom', $5)""",
        uuid7(),
        seeded_app["organization_id"],
        seeded_app["app_id"],
        name,
        status,
    )


async def test_a_blocked_event_is_dropped_and_counted(
    tracker, owner_conn, seeded_app, ingest_redis
):
    await _define(owner_conn, seeded_app, "Noisy Event", status="blocked")
    # No published copy in Redis: the tracker must fall back to the table.
    await ingest_redis.delete(blocked_events_cache_key(str(seeded_app["app_id"])))

    response = await tracker.post(
        "/v1/events",
        json={
            "events": [
                # Matched on the folded form, so capitalisation does not matter.
                sample_event(event_name="noisy_event"),
                sample_event(event_name="level_complete"),
            ]
        },
        headers={"authorization": f"Bearer {seeded_app['api_key']}"},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["accepted"] == 1
    assert body["blocked"] == 1

    # And the fallback published the set for next time.
    published = await ingest_redis.get(blocked_events_cache_key(str(seeded_app["app_id"])))
    assert published is not None and b"Noisy Event" in published


async def test_an_active_definition_blocks_nothing(tracker, owner_conn, seeded_app):
    await _define(owner_conn, seeded_app, "level_complete", status="active")
    response = await tracker.post(
        "/v1/events",
        json={"events": [sample_event(event_name="level_complete")]},
        headers={"authorization": f"Bearer {seeded_app['api_key']}"},
    )
    assert response.status_code == 202
    assert response.json()["blocked"] == 0
    assert response.json()["accepted"] == 1
