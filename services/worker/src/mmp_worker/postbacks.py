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
from collections.abc import Callable
from dataclasses import dataclass, field

import asyncpg
from mmp_attrib.store import CachedAttribution, lookup
from mmp_core.ids import uuid7
from mmp_core.logging import get_logger
from mmp_crypto.envelope import MasterKeyProvider
from mmp_db.jsonfields import decode as decode_json
from mmp_db.jsonfields import decode_list
from mmp_db.pool import Database
from mmp_db.types import DbConn
from mmp_ingest.schema import QueuedEvent, canonical_event_name
from mmp_ingest.stream import ATTRIBUTED_INSTALLS_STREAM, EVENTS_STREAM, StreamConsumer
from mmp_providers.base import PreparedRequest, Provider, ProviderConfig
from mmp_providers.delivery import DeliveryResult, claim, record, send
from mmp_providers.templates import render, variables_from
from redis.asyncio import Redis

from mmp_providers import registry
from mmp_worker.attribution import INSTALL_EVENTS

log = get_logger(__name__)

POSTBACK_GROUP = "postback-sender"

RULES_SQL = """
SELECT r.id, r.method, r.url_template, r.body_template, r.success_status_codes,
       r.requires_attribution, r.is_sandbox, r.organization_id, r.campaign_id,
       i.provider, i.credentials_ciphertext, i.credentials_nonce, i.wrapped_dek,
       -- Aliased: postback_rules carries a key_version of its own now, and two
       -- columns of the same name in one row is a bug waiting for whichever
       -- one the driver happens to keep.
       i.key_version AS integration_key_version,
       i.configuration
FROM postback_rules r
LEFT JOIN provider_integrations i ON i.id = r.provider_integration_id
                                 AND i.status = 'active'
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
    skipped_other_campaign: int = 0
    skipped_unmapped: int = 0
    blocked: int = 0
    by_status: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "considered": self.considered,
            "delivered": self.delivered,
            "failed": self.failed,
            "skipped_unattributed": self.skipped_unattributed,
            "skipped_other_campaign": self.skipped_other_campaign,
            "skipped_unmapped": self.skipped_unmapped,
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
        master_keys: MasterKeyProvider | None = None,
    ) -> None:
        self._redis = redis
        self._database = database
        self._batch_size = batch_size
        self._idle_sleep = idle_sleep
        self._sandbox_endpoint = sandbox_endpoint
        self._master_keys = master_keys
        self._provider: Provider | None = None
        registry.load_builtin_once()
        self._consumer: StreamConsumer[QueuedEvent] = StreamConsumer(
            redis,
            stream=EVENTS_STREAM,
            group=POSTBACK_GROUP,
            consumer=consumer_name,
            decoder_type=QueuedEvent,
        )
        # Installs arrive here only after their attribution is committed; see
        # ATTRIBUTED_INSTALLS_STREAM for the race this replaces.
        self._installs: StreamConsumer[QueuedEvent] = StreamConsumer(
            redis,
            stream=ATTRIBUTED_INSTALLS_STREAM,
            group=POSTBACK_GROUP,
            consumer=consumer_name,
            decoder_type=QueuedEvent,
        )
        self.metrics = PostbackMetrics()
        self._stopping = False

    async def start(self) -> None:
        await self._consumer.ensure_group()
        await self._installs.ensure_group()

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
        events = await self._consumer.read(count=self._batch_size)
        installs = await self._installs.read(count=self._batch_size)
        if not events and not installs:
            return 0

        # Installs on the raw stream are acknowledged without being handled: the
        # same install arrives on the attributed stream once its attribution
        # exists, and handling it here as well is exactly the race that stream
        # removes.
        await self._process(
            self._consumer,
            events,
            skip=lambda event: canonical_event_name(event.event_name) in INSTALL_EVENTS,
        )
        await self._process(self._installs, installs, skip=None)
        return len(events) + len(installs)

    async def _process(
        self,
        consumer: StreamConsumer[QueuedEvent],
        messages: list[tuple[str, QueuedEvent]],
        *,
        skip: Callable[[QueuedEvent], bool] | None,
    ) -> None:
        acked: list[str] = []
        for message_id, event in messages:
            if skip is not None and skip(event):
                acked.append(message_id)
                continue
            try:
                await self.handle(event)
                acked.append(message_id)
            except Exception:
                # Left unacknowledged. The delivery claim makes a redelivery safe:
                # whoever already sent this owns the row, and a retry finds it taken.
                log.exception("postback_handling_failed", event_id=event.event_id)
        await consumer.ack(acked)

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
                    # What the partner put on its tracking link, returned so it can
                    # match the conversion to its own click. Only a campaign-scoped
                    # rule may use these — see _deliver.
                    "sub1": attribution.sub1 if attribution else None,
                    "sub2": attribution.sub2 if attribution else None,
                    "sub3": attribution.sub3 if attribution else None,
                }
            )

            for rule in rules:
                await self._deliver(conn, rule, event, context, attribution)

    def _prepare(
        self, rule: asyncpg.Record, event: QueuedEvent, context: dict[str, object]
    ) -> PreparedRequest | None:
        """Build the request, through an adapter when the rule names one.

        A rule with no provider integration is the plain URL-template path that
        existed before adapters, and stays supported: most affiliate networks
        are exactly that, and requiring an integration record for them would be
        ceremony without benefit.
        """
        self._provider = None

        provider_name = rule.get("provider")
        if not provider_name:
            return PreparedRequest(
                method=rule["method"],
                url=render(rule["url_template"], context),
                headers=({"content-type": "application/json"} if rule["body_template"] else {}),
                body=(
                    render(rule["body_template"], context, encode=False).encode()
                    if rule["body_template"]
                    else None
                ),
                success_codes=tuple(decode_list(rule["success_status_codes"])),
            )

        try:
            provider = registry.get(provider_name)
        except registry.UnknownProvider:
            # An integration naming an adapter this build does not have. Logged
            # loudly: it means a configuration outlived a deployment.
            log.error(
                "postback_provider_unknown",
                provider=provider_name,
                rule_id=str(rule["id"]),
            )
            return None

        self._provider = provider
        config = self._config_for(rule)
        if config is None:
            return None
        return provider.prepare(event_name=event.event_name, context=context, config=config)

    def _config_for(self, rule: asyncpg.Record) -> ProviderConfig | None:
        """Unseal an integration's credentials for one delivery."""
        import msgspec
        from mmp_crypto.envelope import SealedSecret, open_sealed, organization_aad

        if not rule["credentials_ciphertext"]:
            return ProviderConfig(settings=decode_json(rule["configuration"]) or {})
        if self._master_keys is None:
            # Sealed credentials with no key provider to open them. This is a
            # deployment fault, not a per-conversion one, so it is worth being
            # loud about rather than silently degrading every integration on
            # this worker to unauthenticated requests that will all be rejected.
            raise RuntimeError(
                "postback worker has sealed provider credentials but no master "
                "key provider; check the worker's KMS configuration"
            )
        try:
            plaintext = open_sealed(
                SealedSecret(
                    ciphertext=bytes(rule["credentials_ciphertext"]),
                    nonce=bytes(rule["credentials_nonce"]),
                    wrapped_dek=bytes(rule["wrapped_dek"]),
                    key_version=int(rule["integration_key_version"]),
                ),
                provider=self._master_keys,
                aad=organization_aad(rule["organization_id"]),
            )
        except Exception:
            # Without credentials the request cannot be authenticated, and an
            # unauthenticated one would be rejected anyway. Better to skip and
            # say why than to send something that cannot work.
            log.exception("provider_credentials_undecryptable", rule_id=str(rule["id"]))
            return None
        return ProviderConfig(
            credentials=msgspec.json.decode(plaintext, type=dict[str, str]),
            settings=decode_json(rule["configuration"]) or {},
        )

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

        scoped_to = rule["campaign_id"]
        if scoped_to is not None and (
            attribution is None or attribution.campaign_id != str(scoped_to)
        ):
            # A rule for one partner's campaign hears only about that campaign's
            # installs. Before rules could be scoped, every rule fired for every
            # install, so two partners each with a rule were told about each
            # other's conversions — and, once sub1 was available, would have been
            # handed each other's click ids. An organic install belongs to no
            # campaign, so a scoped rule never fires for one, whatever
            # requires_attribution says.
            self.metrics.skipped_other_campaign += 1
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

        # The adapter describes the request; the engine sends it. That split is
        # what keeps the SSRF guard, the delivery claim and the retry policy
        # outside any provider's reach.
        prepared = self._prepare(rule, event, context)
        if prepared is None:
            # The provider does not accept this event. Recorded and abandoned
            # rather than retried: it will not become acceptable later.
            await record(
                conn,
                delivery_id=delivery_id,
                result=DeliveryResult(
                    delivered=False,
                    status_code=None,
                    body=None,
                    error="blocked: provider does not accept this event",
                ),
                success_codes=[],
                attempt=1,
            )
            self.metrics.skipped_unmapped += 1
            return

        url = self._sandbox_endpoint if sandbox else prepared.url
        result = await send(
            url=url,
            method=prepared.method,
            headers=prepared.headers or None,
            body=prepared.body,
            allow_http=sandbox,
        )

        # A provider returning 200 with an error in the body is a rejection
        # wearing a success code. Recording it as delivered is how an advertiser
        # spends a month believing conversions are arriving.
        accepted: bool | None = None
        if result.status_code is not None and self._provider is not None:
            verdict = self._provider.interpret(
                status_code=result.status_code, body=result.body or ""
            )
            accepted = verdict.accepted
            if not verdict.accepted:
                result = DeliveryResult(
                    delivered=False,
                    status_code=result.status_code,
                    body=result.body,
                    error=verdict.detail or "provider rejected the conversion",
                    elapsed_ms=result.elapsed_ms,
                )

        status = await record(
            conn,
            delivery_id=delivery_id,
            result=result,
            success_codes=list(prepared.success_codes),
            attempt=1,
            request_url=url,
            accepted=accepted,
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
                success_codes=decode_list(rule["success_status_codes"]),
                attempt=row["attempt_count"],
            )
            retried += 1

    if retried:
        log.info("postback_retries_attempted", count=retried)
    return retried
