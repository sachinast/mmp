"""Postback delivery: claimed once, retried sensibly, never sent twice."""

from __future__ import annotations

import datetime as dt

import pytest
from mmp_providers.delivery import (
    BACKOFF_BASE,
    MAX_ATTEMPTS,
    MAX_BACKOFF,
    DeliveryResult,
    claim,
    next_retry_at,
    record,
)


async def _rule(owner_conn, seeded_app) -> object:
    from mmp_core.ids import uuid7

    rule_id = uuid7()
    await owner_conn.execute(
        """INSERT INTO postback_rules (id, organization_id, app_id, name, trigger_event,
                                       method, url_template, success_status_codes,
                                       requires_attribution, is_sandbox, enabled,
                                       created_at, updated_at)
           VALUES ($1, $2, $3, 'Network', 'purchase', 'GET',
                   'https://example.com/c?click={{click_id}}', '[200]'::jsonb,
                   true, false, true, now(), now())""",
        rule_id,
        seeded_app["organization_id"],
        seeded_app["app_id"],
    )
    return rule_id


async def test_only_one_worker_claims_a_delivery(owner_conn, seeded_app):
    """The duplicate defence.

    A postback tells an ad network a conversion happened, and networks optimise
    spend on those signals. Sending one twice inflates a campaign's apparent
    performance and, on a cost-per-action deal, means paying twice for one
    action.
    """
    import asyncio

    from mmp_core.ids import uuid7
    from mmp_db.pool import Database

    rule_id = await _rule(owner_conn, seeded_app)
    event_id = uuid7()

    async def worker() -> bool:
        database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
        try:
            async with database.acquire_raw() as conn:
                return await claim(
                    conn,
                    delivery_id=uuid7(),
                    organization_id=seeded_app["organization_id"],
                    rule_id=rule_id,
                    event_id=event_id,
                )
        finally:
            await database.close()

    results = await asyncio.gather(*(worker() for _ in range(8)))
    assert sum(results) == 1, "exactly one worker may own a delivery"

    count = await owner_conn.fetchval(
        "SELECT count(*) FROM postback_deliveries WHERE postback_rule_id = $1", rule_id
    )
    assert count == 1


async def test_a_redelivered_message_does_not_resend(owner_conn, seeded_app):
    """The queue redelivers on timeout by design; that must be a no-op here."""
    from mmp_core.ids import uuid7
    from mmp_db.pool import Database

    rule_id = await _rule(owner_conn, seeded_app)
    event_id = uuid7()

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        async with database.acquire_raw() as conn:
            first = await claim(
                conn,
                delivery_id=uuid7(),
                organization_id=seeded_app["organization_id"],
                rule_id=rule_id,
                event_id=event_id,
            )
            second = await claim(
                conn,
                delivery_id=uuid7(),
                organization_id=seeded_app["organization_id"],
                rule_id=rule_id,
                event_id=event_id,
            )
    finally:
        await database.close()

    assert first
    assert not second


async def test_success_is_recorded_and_not_retried(owner_conn, seeded_app):
    from mmp_core.ids import uuid7
    from mmp_db.pool import Database

    rule_id = await _rule(owner_conn, seeded_app)
    delivery_id, event_id = uuid7(), uuid7()

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        async with database.acquire_raw() as conn:
            await claim(
                conn,
                delivery_id=delivery_id,
                organization_id=seeded_app["organization_id"],
                rule_id=rule_id,
                event_id=event_id,
            )
            status = await record(
                conn,
                delivery_id=delivery_id,
                result=DeliveryResult(True, 200, "ok", None),
                success_codes=[200],
                attempt=1,
            )
    finally:
        await database.close()

    assert status == "delivered"
    row = await owner_conn.fetchrow(
        "SELECT status, delivered_at, next_retry_at FROM postback_deliveries WHERE id = $1",
        delivery_id,
    )
    assert row["status"] == "delivered"
    assert row["delivered_at"] is not None
    assert row["next_retry_at"] is None


async def test_a_definitive_refusal_is_not_retried(owner_conn, seeded_app):
    """A 4xx means the partner understood us and refused.

    Sending the identical request again will be refused identically; retrying
    just burns their rate limit and our workers.
    """
    from mmp_core.ids import uuid7
    from mmp_db.pool import Database

    rule_id = await _rule(owner_conn, seeded_app)
    delivery_id, event_id = uuid7(), uuid7()

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        async with database.acquire_raw() as conn:
            await claim(
                conn,
                delivery_id=delivery_id,
                organization_id=seeded_app["organization_id"],
                rule_id=rule_id,
                event_id=event_id,
            )
            status = await record(
                conn,
                delivery_id=delivery_id,
                result=DeliveryResult(False, 400, "bad request", None),
                success_codes=[200],
                attempt=1,
            )
    finally:
        await database.close()

    assert status == "abandoned"


async def test_a_server_error_is_retried_then_abandoned(owner_conn, seeded_app):
    from mmp_core.ids import uuid7
    from mmp_db.pool import Database

    rule_id = await _rule(owner_conn, seeded_app)
    delivery_id, event_id = uuid7(), uuid7()

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        async with database.acquire_raw() as conn:
            await claim(
                conn,
                delivery_id=delivery_id,
                organization_id=seeded_app["organization_id"],
                rule_id=rule_id,
                event_id=event_id,
            )
            early = await record(
                conn,
                delivery_id=delivery_id,
                result=DeliveryResult(False, 503, "unavailable", None),
                success_codes=[200],
                attempt=1,
            )
            final = await record(
                conn,
                delivery_id=delivery_id,
                result=DeliveryResult(False, 503, "unavailable", None),
                success_codes=[200],
                attempt=MAX_ATTEMPTS,
            )
    finally:
        await database.close()

    assert early == "failed"
    assert final == "abandoned", "retries must be bounded"


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (200, False),
        (204, False),
        (400, False),
        (403, False),
        (404, False),
        (408, True),
        (429, True),
        (500, True),
        (502, True),
        (503, True),
        (None, True),
    ],
)
def test_retry_decisions(status, retryable):
    assert DeliveryResult(False, status, None, None).retryable is retryable


def test_backoff_grows_and_is_capped():
    now = dt.datetime(2026, 9, 8, 12, 0, tzinfo=dt.UTC)
    delays = [(next_retry_at(a, now=now) - now).total_seconds() for a in range(1, 8)]
    # Jitter makes exact values meaningless, so assert the shape.
    assert delays[0] < delays[2] < delays[4]
    assert all(d <= MAX_BACKOFF.total_seconds() * 1.5 for d in delays)
    assert delays[0] >= BACKOFF_BASE.total_seconds() * 0.5


def test_backoff_is_jittered():
    """Without jitter, every worker that failed in the same outage retries at
    the same instant — turning a partner's recovery into a second outage."""
    now = dt.datetime(2026, 9, 8, 12, 0, tzinfo=dt.UTC)
    delays = {next_retry_at(3, now=now) for _ in range(20)}
    assert len(delays) > 1, "retry times must not be identical across workers"


async def test_a_blocked_destination_is_not_retried():
    """Retrying would just re-run the same check with the same answer."""
    from mmp_providers.delivery import send

    result = await send(url="https://169.254.169.254/latest/meta-data/")
    assert not result.delivered
    assert result.error is not None and result.error.startswith("blocked:")
    # No status code normally means "never reached them", which is retryable.
    # A blocked destination is different: it is a permanent verdict, and
    # retrying would re-run the same check for the same answer.
    assert not result.retryable
