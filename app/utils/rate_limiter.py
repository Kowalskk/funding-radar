"""
app/utils/rate_limiter.py — Redis token-bucket rate limiter.

Algorithm
─────────
  Uses a Lua script for atomic evaluation so there are no race conditions
  even across multiple worker processes.

  Bucket state is stored in a single Redis hash:
    Key:    rl:{identifier}
    Fields: tokens (float), last_refill (unix timestamp float)

  On each request:
    1. Compute time since last refill → replenish = elapsed * refill_rate
    2. New tokens = min(capacity, stored_tokens + replenish)
    3. If new_tokens >= 1.0 → consume 1 token, allow
    4. Else → deny (429)

Tier config (all limits are requests per minute, burst = max instantaneous):

  anonymous → capacity=5,   refill=5/min
  free      → capacity=10,  refill=60/min
  pro       → capacity=100, refill=600/min
  custom    → capacity=500, refill=6000/min

The Lua script runs atomically on the Redis server, so this is process-safe.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import NamedTuple

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# ── Tier configuration ────────────────────────────────────────────────────────

class _BucketConfig(NamedTuple):
    capacity: float   # max tokens in bucket (burst)
    refill_per_sec: float  # tokens added per second

_TIER_CONFIG: dict[str, _BucketConfig] = {
    "anonymous": _BucketConfig(capacity=60,  refill_per_sec=120 / 60),
    "free":      _BucketConfig(capacity=50,  refill_per_sec=300 / 60),
    "pro":       _BucketConfig(capacity=200, refill_per_sec=1200 / 60),
    "custom":    _BucketConfig(capacity=1000, refill_per_sec=12000 / 60),
}

_TTL_SECONDS = 120  # expire idle bucket keys after 2 minutes

# ── Lua script ─────────────────────────────────────────────────────────────────
# Returns: {allowed: 0|1, tokens_remaining: float, retry_after_ms: int}
# KEYS[1] = bucket key
# ARGV[1] = capacity   ARGV[2] = refill_per_sec
# ARGV[3] = now (unix float as string)  ARGV[4] = TTL (seconds)

_LUA = """
local key          = KEYS[1]
local capacity     = tonumber(ARGV[1])
local refill_rate  = tonumber(ARGV[2])
local now          = tonumber(ARGV[3])
local ttl          = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens      = tonumber(data[1]) or capacity
local last_refill = tonumber(data[2]) or now

local elapsed  = math.max(0, now - last_refill)
local refilled = elapsed * refill_rate
tokens = math.min(capacity, tokens + refilled)

local allowed = 0
local retry_after_ms = 0

if tokens >= 1.0 then
    tokens  = tokens - 1.0
    allowed = 1
else
    -- milliseconds until 1 token is available
    local deficit = 1.0 - tokens
    retry_after_ms = math.ceil((deficit / refill_rate) * 1000)
end

redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now)
redis.call('EXPIRE', key, ttl)

return {allowed, math.floor(tokens * 1000), retry_after_ms}
"""

# SHA1 of the script for EVALSHA
_LUA_SHA: str | None = None


class TokenBucket:
    """Redis-backed token-bucket rate limiter.

    Usage::

        bucket = TokenBucket(redis_client)
        allowed, retry_after = await bucket.check("user:42", tier="pro")
        if not allowed:
            raise HTTPException(429, headers={"Retry-After": str(retry_after)})
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._sha: str | None = None

    async def _get_sha(self) -> str:
        if self._sha is None:
            self._sha = await self._redis.script_load(_LUA)
        return self._sha

    async def check(
        self,
        identifier: str,
        tier: str = "free",
    ) -> tuple[bool, int]:
        """Check if the request is allowed.

        Returns:
            (allowed: bool, retry_after_seconds: int)
        """
        cfg = _TIER_CONFIG.get(tier, _TIER_CONFIG["free"])
        key = f"rl:{identifier}"
        now = time.time()

        try:
            sha = await self._get_sha()
            result = await self._redis.evalsha(
                sha,
                1,
                key,
                str(cfg.capacity),
                str(cfg.refill_per_sec),
                str(now),
                str(_TTL_SECONDS),
            )
            allowed = bool(result[0])
            retry_after_ms = int(result[2])
            retry_after_sec = max(1, (retry_after_ms + 999) // 1000)
            return allowed, retry_after_sec

        except Exception as exc:
            # On Redis failure, fail open (allow request) to avoid outage
            logger.error("TokenBucket Redis error (fail-open): %s", exc)
            return True, 0
