"""Long-lived tracker resources, built once at startup."""

from __future__ import annotations

from dataclasses import dataclass, field

from mmp_core.logging import get_logger
from mmp_core.ratelimit import RateLimit, RateLimiter
from mmp_core.settings import Settings
from mmp_db.pool import Database
from mmp_ingest.dedup import IdempotencyWindow
from mmp_ingest.stream import EVENTS_STREAM, StreamProducer
from redis.asyncio import Redis

from mmp_tracker.auth import KeyAuthenticator
from mmp_tracker.buffer import ShippingBuffer

log = get_logger(__name__)

# Per app, not per key: an advertiser with ten keys should not get ten times the
# budget. Generous burst because SDKs flush in bursts after a period offline —
# throttling exactly that traffic would punish the reconnect the offline queue
# exists to handle.
DEFAULT_INGEST_LIMIT = RateLimit.per_minute(60_000, burst=5_000)


@dataclass
class TrackerState:
    settings: Settings
    database: Database
    redis: Redis
    authenticator: KeyAuthenticator
    limiter: RateLimiter
    idempotency: IdempotencyWindow
    buffer: ShippingBuffer
    ingest_limit: RateLimit = field(default=DEFAULT_INGEST_LIMIT)
    accepted_total: int = 0

    @classmethod
    async def create(cls, settings: Settings) -> TrackerState:
        # A deliberately small pool: the tracker touches Postgres only on an
        # API-key cache miss. Sizing it like the API's pool would consume
        # connection slots that the workers and the API actually need.
        database = await Database.connect(settings, role="mmp_tracker", min_size=1, max_size=4)
        redis = Redis.from_url(str(settings.redis_url), decode_responses=False)
        producer = StreamProducer(redis, stream=EVENTS_STREAM)
        buffer = ShippingBuffer(producer)
        await buffer.start()
        return cls(
            settings=settings,
            database=database,
            redis=redis,
            authenticator=KeyAuthenticator(database, redis, pepper=settings.api_key_pepper),
            limiter=RateLimiter(redis),
            idempotency=IdempotencyWindow(redis),
            buffer=buffer,
        )

    async def close(self) -> None:
        # Buffer first: it must drain into Redis before Redis is closed.
        await self.buffer.stop()
        await self.redis.aclose()
        await self.database.close()

    async def ping_redis(self) -> None:
        await self.redis.ping()

    async def ping_database(self) -> None:
        await self.database.ping()

    async def ping_buffer(self) -> None:
        """Readiness fails if the buffer is saturated.

        A full buffer means we are already shedding. Reporting not-ready takes
        this instance out of the load balancer so the traffic goes to one that
        can still accept it, instead of being dropped here.
        """
        if self.buffer.depth >= self.buffer._capacity * 0.9:
            raise RuntimeError("ingest buffer is saturated")
