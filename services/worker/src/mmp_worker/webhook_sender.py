"""The webhook consumer.

Delivers to customers' own endpoints, with the same claim-before-send discipline
as postbacks and one thing postbacks do not have: **auto-disable**.

A webhook points at an endpoint the customer operates. Ad networks run
professional infrastructure; a customer's webhook receiver may be a serverless
function someone wrote once and forgot. When it has been failing for long enough
that recovery is not plausible, continuing to retry generates load on a broken
system, a backlog we would have to drain later, and log noise nobody reads. So
after enough consecutive failures the webhook is switched off and the customer
is told — through the dashboard's failure count, which is visible long before
the threshold is reached.

The counter resets on any success, so an endpoint with intermittent trouble is
never disabled by failures spread over weeks.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from urllib.parse import urlparse

from mmp_core.ids import uuid7
from mmp_core.logging import get_logger
from mmp_crypto.envelope import MasterKeyProvider, SealedSecret, open_sealed, organization_aad
from mmp_db.pool import Database
from mmp_db.types import DbConn
from mmp_ingest.schema import QueuedEvent
from mmp_ingest.stream import EVENTS_STREAM, StreamConsumer
from mmp_providers.delivery import DeliveryResult, next_retry_at, send
from mmp_providers.webhooks import (
    MAX_CONSECUTIVE_FAILURES,
    WEBHOOK_EVENTS,
    build_payload,
    signed_headers,
)
from redis.asyncio import Redis

log = get_logger(__name__)

WEBHOOK_GROUP = "webhook-sender"
SUCCESS_CODES = frozenset({200, 201, 202, 204})

SUBSCRIBERS_SQL = """
SELECT id, organization_id, url, secret_ciphertext, secret_nonce, wrapped_dek, key_version,
       consecutive_failures
FROM webhooks
WHERE organization_id = $1 AND enabled AND events ? $2
"""

CLAIM_SQL = """
INSERT INTO webhook_deliveries (
    id, organization_id, webhook_id, event_id, event_type, status,
    attempt_count, request_url, created_at
)
VALUES ($1, $2, $3, $4, $5, 'in_flight', 1, $6, now())
ON CONFLICT (webhook_id, event_id) DO NOTHING
RETURNING id
"""

RECORD_SQL = """
UPDATE webhook_deliveries
SET status = $2::text,
    response_status = $3,
    response_body = $4,
    error = $5,
    delivered_at = CASE WHEN $2::text = 'delivered' THEN now() ELSE delivered_at END,
    next_retry_at = $6
WHERE id = $1
"""

# One statement so the read-modify-write cannot interleave between workers.
# Counting failures in Python and writing the result back would lose increments
# under concurrency, which is how an endpoint that failed fifty times ends up
# reported as having failed twice.
FAILURE_SQL = """
UPDATE webhooks
SET consecutive_failures = consecutive_failures + 1,
    enabled = CASE WHEN consecutive_failures + 1 >= $2 THEN false ELSE enabled END,
    disabled_at = CASE
        WHEN consecutive_failures + 1 >= $2 AND disabled_at IS NULL THEN now()
        ELSE disabled_at
    END,
    updated_at = now()
WHERE id = $1
RETURNING consecutive_failures, enabled
"""

SUCCESS_SQL = """
UPDATE webhooks
SET consecutive_failures = 0, updated_at = now()
WHERE id = $1 AND consecutive_failures > 0
"""


@dataclass
class WebhookMetrics:
    considered: int = 0
    delivered: int = 0
    failed: int = 0
    disabled: int = 0
    by_status: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "considered": self.considered,
            "delivered": self.delivered,
            "failed": self.failed,
            "disabled": self.disabled,
            "by_status": dict(self.by_status),
        }


class WebhookConsumer:
    def __init__(
        self,
        *,
        redis: Redis,
        database: Database,
        consumer_name: str,
        master_keys: MasterKeyProvider,
        batch_size: int = 200,
        idle_sleep: float = 0.1,
    ) -> None:
        self._redis = redis
        self._database = database
        self._master_keys = master_keys
        self._batch_size = batch_size
        self._idle_sleep = idle_sleep
        self._consumer: StreamConsumer[QueuedEvent] = StreamConsumer(
            redis,
            stream=EVENTS_STREAM,
            group=WEBHOOK_GROUP,
            consumer=consumer_name,
            decoder_type=QueuedEvent,
        )
        self.metrics = WebhookMetrics()
        self._stopping = False

    async def start(self) -> None:
        await self._consumer.ensure_group()

    async def stop(self) -> None:
        self._stopping = True

    async def run(self) -> None:
        import asyncio

        await self.start()
        while not self._stopping:
            if await self.run_once() == 0:
                await asyncio.sleep(self._idle_sleep)
        while await self.run_once():
            pass
        log.info("webhook_consumer_drained", **self.metrics.as_dict())

    async def run_once(self) -> int:
        messages = await self._consumer.read(count=self._batch_size)
        if not messages:
            return 0
        acked: list[str] = []
        for message_id, event in messages:
            try:
                await self.handle(event)
                acked.append(message_id)
            except Exception:
                log.exception("webhook_handling_failed", event_id=event.event_id)
        await self._consumer.ack(acked)
        return len(messages)

    async def handle(self, event: QueuedEvent) -> None:
        if event.event_name not in WEBHOOK_EVENTS:
            return

        async with self._database.acquire_raw() as conn:
            subscribers = await conn.fetch(
                SUBSCRIBERS_SQL, uuid.UUID(event.organization_id), event.event_name
            )
            for webhook in subscribers:
                await self._deliver(conn, webhook, event)

    async def _deliver(self, conn: DbConn, webhook: object, event: QueuedEvent) -> None:
        self.metrics.considered += 1
        organization_id = webhook["organization_id"]  # type: ignore[index]
        url = webhook["url"]  # type: ignore[index]
        delivery_id = uuid7()

        claimed = await conn.fetchval(
            CLAIM_SQL,
            delivery_id,
            organization_id,
            webhook["id"],  # type: ignore[index]
            uuid.UUID(event.event_id),
            event.event_name,
            url,
        )
        if claimed is None:
            # Someone already owns this (webhook, event). A duplicated purchase
            # notification is a duplicated order in whatever is listening.
            return

        payload = build_payload(
            event_type=event.event_name,
            data={
                "event_id": event.event_id,
                "app_id": event.app_id,
                "anonymous_id": event.anonymous_id,
                "user_id": event.user_id,
                "occurred_at": event.occurred_at,
                "revenue_minor": event.revenue_minor,
                "currency": event.currency,
                "platform": event.platform,
            },
            delivery_id=delivery_id,
        )
        body = payload.encode()

        try:
            secret = open_sealed(
                SealedSecret(
                    ciphertext=bytes(webhook["secret_ciphertext"]),  # type: ignore[index]
                    nonce=bytes(webhook["secret_nonce"]),  # type: ignore[index]
                    wrapped_dek=bytes(webhook["wrapped_dek"]),  # type: ignore[index]
                    # From the row, not a constant. A secret sealed after a
                    # master key rotation is wrapped under the new key, and
                    # unwrapping it under the old one fails outright.
                    key_version=int(webhook["key_version"]),  # type: ignore[index]
                ),
                provider=self._master_keys,
                aad=organization_aad(organization_id),
            ).decode("utf-8")
        except Exception:
            # Without the secret we cannot sign, and an unsigned delivery is one
            # the receiver has no reason to trust. Abandoned rather than sent.
            log.exception("webhook_secret_undecryptable", webhook_id=str(webhook["id"]))  # type: ignore[index]
            await conn.execute(
                RECORD_SQL,
                delivery_id,
                "abandoned",
                None,
                None,
                "signing secret could not be decrypted",
                None,
            )
            return

        result = await send(
            url=url,
            method="POST",
            headers=signed_headers(
                payload=body,
                secret=secret,
                url_path=urlparse(url).path or "/",
                delivery_id=str(delivery_id),
            ),
            body=body,
        )
        await self._record(conn, webhook, delivery_id, result)

    async def _record(
        self, conn: DbConn, webhook: object, delivery_id: uuid.UUID, result: DeliveryResult
    ) -> None:
        delivered = result.status_code in SUCCESS_CODES

        if delivered:
            status_name = "delivered"
            retry_at = None
        elif not result.retryable:
            status_name = "abandoned"
            retry_at = None
        else:
            status_name = "failed"
            retry_at = next_retry_at(1)

        await conn.execute(
            RECORD_SQL,
            delivery_id,
            status_name,
            result.status_code,
            (result.body or "")[:2000] or None,
            result.error,
            retry_at,
        )

        webhook_id = webhook["id"]  # type: ignore[index]
        if delivered:
            self.metrics.delivered += 1
            # Reset on any success, so intermittent trouble spread over weeks
            # never accumulates into a disable.
            await conn.execute(SUCCESS_SQL, webhook_id)
        else:
            self.metrics.failed += 1
            row = await conn.fetchrow(FAILURE_SQL, webhook_id, MAX_CONSECUTIVE_FAILURES)
            if row is not None and not row["enabled"]:
                self.metrics.disabled += 1
                log.warning(
                    "webhook_auto_disabled",
                    webhook_id=str(webhook_id),
                    consecutive_failures=row["consecutive_failures"],
                )

        self.metrics.by_status[status_name] = self.metrics.by_status.get(status_name, 0) + 1
