"""
app/dependencies.py — Application-wide FastAPI dependencies.

Provides:
  get_current_user()    — extract User from Bearer JWT or X-API-Key header
  require_pro()         — assert tier is pro or custom
  get_token_bucket()    — shared Redis TokenBucket instance
  redis_rate_limit()    — Depends-compatible rate limiter using TokenBucket
"""

# NOTE: Do NOT use `from __future__ import annotations` here.
# It breaks FastAPI's dependency injection for Request, Depends, etc.

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from jose import ExpiredSignatureError, JWTError
from redis.asyncio import Redis

from app.core.redis import get_redis
from app.models.db.user import User, UserTier
from app.services.auth_service import decode_token
from app.utils.rate_limiter import TokenBucket

# ── Shared TokenBucket singleton ──────────────────────────────────────────────

_token_bucket: TokenBucket | None = None


def get_token_bucket(redis: Redis = Depends(get_redis)) -> TokenBucket:
    global _token_bucket
    if _token_bucket is None:
        _token_bucket = TokenBucket(redis)
    return _token_bucket


# ── User resolution ───────────────────────────────────────────────────────────


async def _resolve_user_from_jwt(token: str) -> User | None:
    """Decode Bearer JWT and fetch the User from DB."""
    try:
        payload = decode_token(token)
    except ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except JWTError:
        return None  # fall through to API key path

    user_id_str = payload.get("sub")
    if not user_id_str:
        return None

    from app.core.database import get_db_session
    from sqlalchemy import select

    async with get_db_session() as session:
        return await session.scalar(
            select(User).where(User.id == int(user_id_str), User.is_active.is_(True))
        )


async def _resolve_user_from_api_key(api_key: str) -> User | None:
    """Check Redis cache first, then DB."""
    redis = get_redis()

    # Fast path: Redis cache
    cached_tier = await redis.get(f"auth:tier:{api_key}")

    from app.core.database import get_db_session
    from sqlalchemy import select

    async with get_db_session() as session:
        user = await session.scalar(
            select(User).where(User.api_key == api_key, User.is_active.is_(True))
        )

    if user and not cached_tier:
        # Populate the tier + reverse-lookup caches
        await redis.set(f"auth:tier:{api_key}", user.tier.value, ex=300)
        await redis.set(f"auth:user_api_key:{user.id}", api_key, ex=300)

    return user


async def get_current_user(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> User:
    """Resolve the authenticated User from Bearer JWT or X-API-Key.

    Raises 401 if neither is present or valid.
    """
    user: User | None = None

    # 1. Bearer JWT
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        user = await _resolve_user_from_jwt(token)

    # 2. API key header
    if user is None and x_api_key:
        user = await _resolve_user_from_api_key(x_api_key)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Provide a Bearer token or X-API-Key header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


async def get_optional_user(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> User | None:
    """Like get_current_user but returns None instead of raising 401."""
    try:
        return await get_current_user(request, authorization, x_api_key)
    except HTTPException:
        return None


async def require_pro(user: User = Depends(get_current_user)) -> User:
    """Assert that the authenticated user is on the Pro or Custom tier."""
    if user.tier not in (UserTier.PRO, UserTier.CUSTOM):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires a Pro subscription.",
        )
    return user


# ── Redis token-bucket rate limiting ─────────────────────────────────────────


class RedisBucketLimiter:
    """FastAPI callable dependency using the Redis token bucket.

    Usage::

        @router.get("/endpoint")
        async def ep(_rl: None = Depends(RedisBucketLimiter())):
            ...
    """

    async def __call__(
        self,
        request: Request,
        user: User | None = Depends(get_optional_user),
        bucket: TokenBucket = Depends(get_token_bucket),
    ) -> None:
        tier = user.tier.value if user else "anonymous"
        client_ip = request.client.host if request.client else "unknown"
        # Prefer user-scoped key to prevent shared-IP collisions
        identifier = f"user:{user.id}" if user else f"ip:{client_ip}"

        allowed, retry_after = await bucket.check(identifier, tier=tier)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded. Tier: {tier}.",
                headers={"Retry-After": str(retry_after)},
            )


# Singleton dependency to reuse in all routers
redis_rate_limit = RedisBucketLimiter()
