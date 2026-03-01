"""
app/api/deps.py — FastAPI dependency injection helpers.

Provides:
  - get_redis_client        → shared Redis instance
  - get_funding_service_dep → FundingService singleton
  - get_current_user_tier   → resolves tier from X-API-Key header (or "anonymous")
  - rate_limit              → RateLimiter singleton backed by Redis token-bucket
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from redis.asyncio import Redis

from app.core.redis import get_redis
from app.services.funding_service import FundingService

# ── Tier limits (for reference / OpenAPI docs) ────────────────────────────────

TIER_LIMITS: dict[str, int] = {
    "anonymous": 30,
    "free": 60,
    "pro": 600,
    "custom": 6000,
}

# ── Redis dependency ──────────────────────────────────────────────────────────


async def get_redis_client() -> Redis:
    """Returns the module-level shared async Redis client."""
    return get_redis()


# ── FundingService dependency ─────────────────────────────────────────────────


async def get_funding_service_dep() -> FundingService:
    """Returns the running FundingService instance."""
    from app.main import get_funding_service
    return get_funding_service()


# ── API key → tier resolver ────────────────────────────────────────────────────


async def get_current_user_tier(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    redis: Redis = Depends(get_redis_client),
) -> str:
    """Resolve caller's tier from the X-API-Key header.

    - No header → ``"anonymous"``
    - Valid key  → ``"free"`` | ``"pro"`` | ``"custom"``
    - Invalid    → 401 Unauthorized
    """
    if x_api_key is None:
        return "anonymous"

    cached = await redis.get(f"auth:tier:{x_api_key}")
    if cached:
        return cached if isinstance(cached, str) else cached.decode()

    from app.core.database import get_db_session
    from app.models.db.user import User
    from sqlalchemy import select

    async with get_db_session() as session:
        result = await session.execute(
            select(User.tier).where(
                User.api_key == x_api_key,
                User.is_active.is_(True),
            )
        )
        row = result.first()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    tier: str = row[0].value
    await redis.set(f"auth:tier:{x_api_key}", tier, ex=300)
    return tier


# ── Rate limiter — Redis token-bucket (multi-process safe) ────────────────────


class RateLimiter:
    """FastAPI callable dependency backed by the Redis token bucket.

    Delegates to ``app.utils.rate_limiter.TokenBucket`` which uses an atomic
    Lua script, so this is safe across multiple Uvicorn workers.

    Usage::

        @router.get("/endpoint")
        async def endpoint(_rl: None = Depends(rate_limit)):
            ...
    """

    async def __call__(
        self,
        request: Request,
        tier: str = Depends(get_current_user_tier),
    ) -> None:
        from app.dependencies import get_token_bucket

        bucket = get_token_bucket()
        client_ip = request.client.host if request.client else "unknown"
        identifier = f"ip:{client_ip}:{tier}"

        allowed, retry_after = await bucket.check(identifier, tier=tier)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded for tier '{tier}'.",
                headers={"Retry-After": str(retry_after)},
            )


# Module-level singleton — import and use as Depends(rate_limit)
rate_limit = RateLimiter()
