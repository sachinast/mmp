"""The postback consumer.

Reads conversions off the event stream through its own group, finds the rules
that match, resolves the attribution, and delivers. Independent of persistence
and attribution so that a slow write batch never delays a conversion reaching an
ad network — networks optimise spend on these signals, and a late one is spend
misallocated.

Sandbox rules deliver to an internal echo endpoint instead of the real one. That
is not a convenience: without it, the only way to test a postback configuration
is to send a fabricated conversion to a live ad account, which corrupts the
campaign reporting the customer is paying us to produce.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import asyncpg
from mmp_attrib.store import CachedAttribution, lookup
from mmp_core.ids import uuid7
from mmp_core.logging import get_logger
from mmp_db.pool import Database
from mmp_db.types import DbConn
from mmp_ingest.schema import QueuedEvent
from mmp_ingest.stream import EVENTS_STREAM, StreamConsumer
from mmp_providers.delivery import claim, record, send
from mmp_providers.templates import render, variables_from
from redis.asyncio import Redis

log = get_logger(__name__)

POSTBACK_GROUP = "postback-sender"

RULES_SQL = """
SELECT r.id, r.method, r.url_template, r.body_template, r.success_status_codes,
       r.requires_attribution, r.is_sandbox, r.organization_id
FROM postback_rules r
WHERE r.app_id = $1 AND r.trigger_event = $2 AND r.enabled
"""

CAMPAIGN_NAME_SQL = "SELECT name, source, medium FROM campaigns WHERE id = $1"

# Where a sandbox rule's traffic goes instead of the partner. Internal, so http
# is acceptable and validated separately from customer-supplied destinations.
SANDBOX_ENDPOINT = "http://127.0.0.1:8002/v1/internal/postback-echo"


@dataclass
class PostbackMetrics:
    considered: int = 0
    delivered: int = 0
    failed: int = 0
    skipped_unattributed: int = 0
    blocked: int = 0
    by_status: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "considered": self.considered,
            "delivered": self.delivered,
            "failed": self.failed,
            "skipped_unattributed": self.skipped_unattributed,
            "blocked": self.blocked,
            "by_status": dict(self.by_status),
        }


class PostbackConsumer:
    def __init__(
        self,
        *,
        redis: Redis,
        database: Database,
        consumer_name: str,
        batch_size: int = 200,
        idle_sleep: float = 0.1,
        sandbox_endpoint: str = SANDBOX_ENDPOINT,
    ) -> None:
        self._redis = redis
        self._database = database
        self._batch_size = batch_size
        self._idle_sleep = idle_sleep
        self._sandbox_endpoint = sandbox_endpoint
        self._consumer: StreamConsumer[QueuedEvent] = StreamConsumer(
            redis,
            stream=EVENTS_STREAM,
            group=POSTBACK_GROUP,
            consumer=consumer_name,
            decoder_type=QueuedEvent,
        )
        self.metrics = PostbackMetrics()
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
        log.info("postback_consumer_drained", **self.metrics.as_dict())

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
                # Left unacknowledged for redelivery. The claim below makes a
                # redelivery safe: whoever already sent this owns the row, and
                # the retry finds it taken.
                log.exception("postback_handling_failed", event_id=event.event_id)
        await self._consumer.ack(acked)
        return len(messages)

    async def handle(self, event: QueuedEvent) -> None:
        app_id = uuid.UUID(event.app_id)

        async with self._database.acquire_raw() as conn:
            rules = await conn.fetch(RULES_SQL, app_id, event.event_name)
            if not rules:
                return

            attribution = await lookup(
                self._redis, conn, app_id=app_id, anonymous_id=event.anonymous_id
            )
            campaign_name = source = medium = None
            if attribution and attribution.campaign_id:
                row = await conn.fetchrow(CAMPAIGN_NAME_SQL, uuid.UUID(attribution.campaign_id))
                if row:
                    campaign_name, source, medium = row["name"], row["source"], row["medium"]

            context = variables_from(
                {
                    "event_id": event.event_id,
                    "event_name": event.event_name,
                    "event_timestamp": event.occurred_at,
                    "app_id": event.app_id,
                    "user_id": event.user_id,
                    "anonymous_id": event.anonymous_id,
                    "click_id": attribution.click_id if attribution else None,
                    "campaign_id": attribution.campaign_id if attribution else None,
                    "campaign_name": campaign_name,
                    "source": source,
                    "medium": medium,
                    # Major units, because that is what every network's macro
                    # expects. Converted once, here, from the integer minor
                    # units everything else uses.
                    "revenue": (
                        f"{event.revenue_minor / 100:.2f}"
                        if event.revenue_minor is not None
                        else None
                    ),
                    "currency": event.currency,
                    "platform": event.platform,
                    "attribution_method": attribution.method if attribution else "organic",
                }
            )

            for rule in rules:
                await self._deliver(conn, rule, event, context, attribution)

    async def _deliver(
        self,
        conn: DbConn,
        rule: asyncpg.Record,
        event: QueuedEvent,
        context: dict[str, object],
        attribution: CachedAttribution | None,
    ) -> None:
        self.metrics.considered += 1

        attributed = attribution is not None and attribution.click_id is not None
        if rule["requires_attribution"] and not attributed:
            # Reporting an unattributed conversion to a network would credit it
            # for something it did not cause.
            self.metrics.skipped_unattributed += 1
            return

        delivery_id = uuid7()
        claimed = await claim(
            conn,
            delivery_id=delivery_id,
            organization_id=rule["organization_id"],
            rule_id=rule["id"],
            event_id=uuid.UUID(event.event_id),
        )
        if not claimed:
            # Someone already owns this delivery. Exactly what should happen on
            # a redelivered queue message.
            return

        sandbox = rule["is_sandbox"]
        url = self._sandbox_endpoint if sandbox else render(rule["url_template"], context)
        body = (
            render(rule["body_template"], context, encode=False).encode()
            if rule["body_template"]
            else None
        )

        result = await send(
            url=url,
            method=rule["method"],
            headers={"content-type": "application/json"} if body else None,
            body=body,
            allow_http=sandbox,
        )
        status = await record(
            conn,
            delivery_id=delivery_id,
            result=result,
            success_codes=list(rule["success_status_codes"]),
            attempt=1,
            request_url=url,
        )

        self.metrics.by_status[status] = self.metrics.by_status.get(status, 0) + 1
        if status == "delivered":
            self.metrics.delivered += 1
        elif result.error and result.error.startswith("blocked:"):
            self.metrics.blocked += 1
            log.error(
                "postback_destination_blocked",
                rule_id=str(rule["id"]),
                error=result.error,
            )
        else:
            self.metrics.failed += 1

        log.info(
            "postback_attempted",
            rule_id=str(rule["id"]),
            event_id=event.event_id,
            status=status,
            response_status=result.status_code,
            sandbox=sandbox,
            elapsed_ms=result.elapsed_ms,
        )


async def retry_due(database: Database, *, limit: int = 100) -> int:
    """Re-attempt deliveries whose backoff has elapsed.

    Separate from the consumer because a retry is not driven by a queue message:
    that message was acknowledged when the first attempt was recorded. This is
    what turns a partner's outage into a delay rather than a loss.

    ``FOR UPDATE SKIP LOCKED`` lets several workers share the backlog without
    two of them claiming the same row — the same problem the delivery claim
    solves for first attempts, at a different point in the lifecycle.
    """
    claim_sql = """
        UPDATE postback_deliveries d
        SET status = 'in_flight', attempt_count = d.attempt_count + 1
        WHERE d.id IN (
            SELECT id FROM postback_deliveries
            WHERE status = 'failed'
              AND next_retry_at IS NOT NULL
              AND next_retry_at <= now()
            ORDER BY next_retry_at
            LIMIT $1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING d.id, d.attempt_count, d.request_url, d.postback_rule_id
    """
    rule_sql = """
        SELECT method, success_status_codes, is_sandbox
        FROM postback_rules WHERE id = $1
    """

    retried = 0
    async with database.system_connection() as conn:
        rows = await conn.fetch(claim_sql, limit)
        for row in rows:
            if not row["request_url"]:
                # Nothing to re-send. Abandon rather than loop forever on a row
                # that can never succeed.
                await conn.execute(
                    "UPDATE postback_deliveries SET status = 'abandoned', "
                    "error = 'no stored request URL to retry' WHERE id = $1",
                    row["id"],
                )
                continue

            rule = await conn.fetchrow(rule_sql, row["postback_rule_id"])
            if rule is None:
                await conn.execute(
                    "UPDATE postback_deliveries SET status = 'abandoned', "
                    "error = 'rule no longer exists' WHERE id = $1",
                    row["id"],
                )
                continue

            result = await send(
                url=row["request_url"],
                method=rule["method"],
                allow_http=rule["is_sandbox"],
            )
            await record(
                conn,
                delivery_id=row["id"],
                result=result,
                success_codes=list(rule["success_status_codes"]),
                attempt=row["attempt_count"],
            )
            retried += 1

    if retried:
        log.info("postback_retries_attempted", count=retried)
    return retried
