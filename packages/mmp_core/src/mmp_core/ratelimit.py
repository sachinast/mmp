"""Token-bucket rate limiting in Redis.

Implemented as a Lua script so that the read, the refill calculation and the
write are one atomic operation on the Redis side. The obvious Python version —
GET, compute, SET — has a race between the GET and the SET that lets concurrent
requests each see the same remaining budget. Under exactly the burst the limiter
exists to stop, it fails open.

Token bucket rather than a fixed window because a fixed window permits double
the intended rate across a boundary: a caller spends the whole budget at
:59.9 and the whole next budget at :00.1.
"""

from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis

# KEYS[1] bucket, ARGV: capacity, refill_per_second, now_ms, cost
_SCRIPT = """
local bucket = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])

local state = redis.call('HMGET', bucket, 'tokens', 'updated')
local tokens = tonumber(state[1])
local updated = tonumber(state[2])

if tokens == nil then
    tokens = capacity
    updated = now
end

local elapsed = math.max(0, now - updated) / 1000.0
tokens = math.min(capacity, tokens + elapsed * refill)

local allowed = 0
if tokens >= cost then
    tokens = tokens - cost
    allowed = 1
end

redis.call('HMSET', bucket, 'tokens', tokens, 'updated', now)
-- Expire an idle bucket once it would have refilled completely; keeping every
-- bucket forever would make the limiter a memory leak keyed by attacker input.
redis.call('PEXPIRE', bucket, math.ceil((capacity / refill) * 1000) + 1000)

local retry_after = 0
if allowed == 0 then
    retry_after = math.ceil(((cost - tokens) / refill) * 1000)
end
return {allowed, math.floor(tokens), retry_after}
"""


@dataclass(frozen=True)
class RateLimit:
    capacity: int
    refill_per_second: float

    @classmethod
    def per_minute(cls, count: int, *, burst: int | None = None) -> RateLimit:
        return cls(capacity=burst or count, refill_per_second=count / 60.0)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after_ms: int


class RateLimiter:
    def __init__(self, redis: Redis, *, prefix: str = "rl:") -> None:
        self._redis = redis
        self._prefix = prefix
        self._script = redis.register_script(_SCRIPT)

    async def check(
        self, key: str, limit: RateLimit, *, cost: int = 1, now_ms: int | None = None
    ) -> RateLimitResult:
        import time

        now = now_ms if now_ms is not None else int(time.time() * 1000)
        allowed, remaining, retry_after = await self._script(
            keys=[self._prefix + key],
            args=[limit.capacity, limit.refill_per_second, now, cost],
        )
        return RateLimitResult(
            allowed=bool(allowed), remaining=int(remaining), retry_after_ms=int(retry_after)
        )


# Login is the endpoint an attacker will actually hammer. Limited on both axes:
# per account, so one victim cannot be brute-forced; and per source address, so
# one attacker cannot spray many accounts at one guess each.
LOGIN_PER_ACCOUNT = RateLimit.per_minute(5, burst=5)
LOGIN_PER_ADDRESS = RateLimit.per_minute(20, burst=20)
