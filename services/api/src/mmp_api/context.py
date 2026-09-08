"""Application context: the long-lived resources a request needs.

Built once at startup, torn down in reverse. Kept out of module globals so that
tests can construct one against their own database and Redis without patching
imports.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from mmp_core.ratelimit import RateLimiter
from mmp_core.settings import Settings
from mmp_crypto.envelope import MasterKeyProvider
from mmp_crypto.kms import provider_from_settings
from mmp_db.pool import Database
from redis.asyncio import Redis

from mmp_api.sessions import SessionStore


@dataclass
class AppContext:
    settings: Settings
    database: Database
    redis: Redis
    sessions: SessionStore
    limiter: RateLimiter
    master_keys: MasterKeyProvider

    @classmethod
    async def create(cls, settings: Settings) -> AppContext:
        database = await Database.connect(settings, role="mmp_api")
        redis = Redis.from_url(str(settings.redis_url), decode_responses=True)
        return cls(
            settings=settings,
            database=database,
            redis=redis,
            sessions=SessionStore(redis),
            limiter=RateLimiter(redis),
            master_keys=_master_key_provider(settings),
        )

    async def close(self) -> None:
        await self.redis.aclose()
        await self.database.close()

    async def ping_database(self) -> None:
        await self.database.ping()

    async def ping_redis(self) -> None:
        await self.redis.ping()


def _master_key_provider(settings: Settings) -> MasterKeyProvider:
    """Kept as a thin alias so existing call sites read naturally.

    The derivation itself lives in mmp_crypto, shared with the worker — see
    provider_from_settings for why that matters.
    """
    return provider_from_settings(settings)


def redis_url_for_tests() -> str:
    return os.environ.get("MMP_REDIS_URL", "redis://127.0.0.1:6379/1")
