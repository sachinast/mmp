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
from mmp_crypto.envelope import LocalMasterKeyProvider, MasterKeyProvider
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
    """Resolve the credential-wrapping key.

    In production this returns a KMS-backed provider (Phase 11). Until then the
    local provider derives a key from configuration — and refuses to do so
    outside development, so the weaker path cannot reach production by accident.
    """
    if settings.is_prod:
        raise RuntimeError(
            "no KMS master key provider configured — refusing to start in production "
            "with process-local credential encryption"
        )
    from hashlib import sha256

    material = sha256(settings.session_secret.encode()).digest()
    return LocalMasterKeyProvider({1: material}, 1)


def redis_url_for_tests() -> str:
    return os.environ.get("MMP_REDIS_URL", "redis://127.0.0.1:6379/1")
