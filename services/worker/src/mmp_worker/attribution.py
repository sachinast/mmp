"""The attribution consumer.

Reads the event stream through its own consumer group, so attribution runs
independently of persistence: a slow or failing write batch does not delay
attribution, and vice versa. Both groups see every message; this one acts only
on installs.

Ordering is worth being explicit about. This consumer may attribute an install
before the event row itself is written, and that is fine — attribution reads
*clicks*, not events. What it must not do is attribute an install before the
click that caused it has been persisted, which is why clicks have their own
consumer and their own buffer, and why a late click shows up as an organic
install that a later referrer can supersede rather than as a lost one.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from mmp_attrib.candidates import load_candidates
from mmp_attrib.fraud import InstallContext, Verdict, assess_install
from mmp_attrib.store import link_user, record
from mmp_core.logging import get_logger
from mmp_core.metrics import attributions, fraud_verdicts
from mmp_db.pool import Database
from mmp_ingest.schema import QueuedEvent, canonical_event_name
from mmp_ingest.stream import (
    ATTRIBUTED_INSTALLS_STREAM,
    EVENTS_STREAM,
    StreamConsumer,
    StreamProducer,
)
from redis.asyncio import Redis

from mmp_attrib import Install, Method, attribute, parse_referrer

log = get_logger(__name__)

ATTRIBUTION_GROUP = "attribution-writer"

# Events that create or update an attribution. Everything else is a conversion,
# which *reads* an attribution rather than producing one.
# Compared against `canonical_event_name(...)`, never against the raw name.
# "Install", "install" and "INSTALL" are the same intent, and an app that gets
# the casing wrong should not silently go unattributed.
INSTALL_EVENTS = frozenset({"install"})
IDENTITY_EVENTS = frozenset({"login", "signup"})

# How long an app's attribution settings may be stale. Short enough that a
# window someone just changed starts applying while they are still looking at
# the screen; long enough that a busy app is not re-reading the same row on
# every install.
APP_CONFIG_TTL = dt.timedelta(seconds=60)

APP_CONFIG_SQL = """
SELECT organization_id, install_window_days, event_window_days
FROM apps WHERE id = $1
"""


@dataclass
class AttributionMetrics:
    processed: int = 0
    attributed: int = 0
    organic: int = 0
    superseded: int = 0
    injections_flagged: int = 0
    by_method: dict[str, int] = field(default_factory=dict)
    by_verdict: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "processed": self.processed,
            "attributed": self.attributed,
            "organic": self.organic,
            "superseded": self.superseded,
            "injections_flagged": self.injections_flagged,
            "by_method": dict(self.by_method),
            "by_verdict": dict(self.by_verdict),
            # The number a customer notices first. Reported so a drop is visible
            # in the logs before an advertiser reports it to us.
            "match_rate": (round(self.attributed / self.processed, 4) if self.processed else None),
        }


class AttributionConsumer:
    def __init__(
        self,
        *,
        redis: Redis,
        database: Database,
        consumer_name: str,
        batch_size: int = 500,
        idle_sleep: float = 0.1,
        config_ttl: dt.timedelta = APP_CONFIG_TTL,
    ) -> None:
        self._redis = redis
        self._database = database
        self._batch_size = batch_size
        self._idle_sleep = idle_sleep
        self._config_ttl = config_ttl
        self._consumer: StreamConsumer[QueuedEvent] = StreamConsumer(
            redis,
            stream=EVENTS_STREAM,
            group=ATTRIBUTION_GROUP,
            consumer=consumer_name,
            decoder_type=QueuedEvent,
        )
        # Config, and when it goes stale. Bounded rather than permanent: an
        # attribution window a customer changed has to start applying without
        # anyone restarting a worker.
        self._app_config: dict[uuid.UUID, tuple[tuple[uuid.UUID, int, int], dt.datetime]] = {}
        self._attributed = StreamProducer(redis, stream=ATTRIBUTED_INSTALLS_STREAM)
        self.metrics = AttributionMetrics()
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
        log.info("attribution_consumer_drained", **self.metrics.as_dict())

    async def run_once(self) -> int:
        messages = await self._consumer.read(count=self._batch_size)
        if not messages:
            return 0

        acked: list[str] = []
        for message_id, event in messages:
            try:
                await self._handle(event)
                acked.append(message_id)
            except Exception:
                # Left unacknowledged so it is retried. One event that cannot be
                # attributed must not stop the rest of the batch from being
                # acknowledged — the alternative is that a single bad install
                # blocks every attribution behind it.
                log.exception(
                    "attribution_failed",
                    event_id=event.event_id,
                    event_name=event.event_name,
                )
        await self._consumer.ack(acked)
        return len(messages)

    async def _config(self, app_id: uuid.UUID) -> tuple[uuid.UUID, int, int] | None:
        """App attribution settings, cached briefly.

        Cached for the process's lifetime until this had an expiry, which meant
        an advertiser could change their install window through the API, see the
        new value returned, see it in the dashboard, and have attribution keep
        using the old one until someone restarted the worker. An install outside
        the old window and inside the new one was reported organic — a
        measurement error, invisible, on a number they are billed for.

        A TTL rather than LISTEN/NOTIFY, unlike the tracker's link cache. That
        cache sits in a 2 ms redirect where a query per lookup would show, and
        needs "I disabled that link" to mean something within milliseconds. This
        is a worker that already does a database round trip per install; one
        more per app per minute is not worth a listener connection to avoid.

        Windows are still copied onto each attribution row, so a stale value
        cannot corrupt history — but it can no longer quietly ignore a change
        either.
        """
        cached = self._app_config.get(app_id)
        now = dt.datetime.now(dt.UTC)
        if cached is not None and cached[1] > now:
            return cached[0]

        async with self._database.acquire_raw() as conn:
            row = await conn.fetchrow(APP_CONFIG_SQL, app_id)
        if row is None:
            return None
        config = (
            row["organization_id"],
            row["install_window_days"],
            row["event_window_days"],
        )
        self._app_config[app_id] = (config, now + self._config_ttl)
        return config

    async def _handle(self, event: QueuedEvent) -> None:
        canonical = canonical_event_name(event.event_name)
        if canonical in IDENTITY_EVENTS and event.user_id:
            await link_user(
                self._redis,
                app_id=uuid.UUID(event.app_id),
                anonymous_id=event.anonymous_id,
                user_id=event.user_id,
            )
            return

        if canonical not in INSTALL_EVENTS:
            return

        app_id = uuid.UUID(event.app_id)
        config = await self._config(app_id)
        if config is None:
            log.warning("attribution_unknown_app", app_id=event.app_id)
            return
        organization_id, install_window, event_window = config

        installed_at = dt.datetime.fromisoformat(event.received_at)
        properties = event.properties or {}
        parsed = parse_referrer(properties.get("install_referrer"))

        sdk_click_id: uuid.UUID | None = None
        if event.click_id:
            try:
                sdk_click_id = uuid.UUID(event.click_id)
            except ValueError:
                sdk_click_id = None

        # The advertising ID was hashed at the edge and the raw value discarded;
        # what reaches here is the digest, hex-encoded so it survives JSON.
        device_hash: bytes | None = None
        digest = properties.get("device_hash")
        if isinstance(digest, str):
            try:
                device_hash = bytes.fromhex(digest)
            except ValueError:
                device_hash = None

        click_ids = [cid for cid in (parsed.click_id, sdk_click_id) if cid is not None]

        async with self._database.acquire_raw() as conn:
            candidates = await load_candidates(
                conn,
                app_id=app_id,
                installed_at=installed_at,
                window_days=install_window,
                click_ids=click_ids,
                device_hash=device_hash,
            )

            decision = attribute(
                Install(
                    app_id=app_id,
                    anonymous_id=event.anonymous_id,
                    installed_at=installed_at,
                    referrer=properties.get("install_referrer"),
                    click_id=sdk_click_id,
                    device_hash=device_hash,
                ),
                candidates,
                window_days=install_window,
                referrer_click_id=parsed.click_id,
            )

            # Assessed here rather than in a later sweep because everything
            # these rules need is already in hand — the winning click and the
            # gap — so it costs no query, and a verdict that arrives with the
            # attribution can be acted on before the conversion is paid for.
            assessment = assess_install(
                InstallContext(
                    click_to_install=decision.click_to_install,
                    click_is_bot=decision.click.is_bot if decision.click else False,
                )
            )

            stored = await record(
                conn,
                self._redis,
                organization_id=organization_id,
                app_id=app_id,
                anonymous_id=event.anonymous_id,
                user_id=event.user_id,
                installed_at=installed_at,
                decision=decision,
                event_window_days=event_window,
                assessment=assessment,
            )

        # After the transaction above has committed, so the postback sender can
        # always find what it is about to report. Published for organic installs
        # too — an app-wide rule that does not require attribution still wants
        # them — and again on a redelivered install: postback delivery claims are
        # unique per rule and event, so a repeat cannot send twice.
        await self._attributed.publish([event])

        self.metrics.processed += 1
        method = str(decision.method)
        self.metrics.by_method[method] = self.metrics.by_method.get(method, 0) + 1
        attributions.labels(method=method).inc()
        if decision.method is Method.ORGANIC:
            self.metrics.organic += 1
        else:
            self.metrics.attributed += 1
        if stored.superseded is not None:
            self.metrics.superseded += 1
        if assessment.verdict is not Verdict.CLEAN:
            self.metrics.by_verdict[str(assessment.verdict)] = (
                self.metrics.by_verdict.get(str(assessment.verdict), 0) + 1
            )
            # No tenant label: this counter is scraped by Prometheus, and a
            # per-organisation label here would put customer cardinality into
            # a metrics store that keeps it forever.
            fraud_verdicts.labels(verdict=str(assessment.verdict)).inc()
            log.warning(
                "fraud_signals_raised",
                app_id=event.app_id,
                verdict=str(assessment.verdict),
                score=assessment.score,
                rules=assessment.rules,
            )

        if decision.suspected_injection:
            self.metrics.injections_flagged += 1
            log.warning(
                "click_injection_suspected",
                app_id=event.app_id,
                click_id=str(decision.click_id),
                click_to_install_ms=int(decision.click_to_install.total_seconds() * 1000)
                if decision.click_to_install
                else None,
            )
