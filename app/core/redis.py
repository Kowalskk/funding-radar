"""
app/core/redis.py — Async Redis connection pool and pub/sub helpers.
"""

from __future__ import annotations

import logging
from typing import Any

import redis.asyncio as aioredis
from redis.asyncio import Redis
from redis.asyncio.connection import ConnectionPool
from redis.exceptions import RedisError

from app.config import Settings

logger = logging.getLogger(__name__)

# ── Module-level singleton ────────────────────────────────────────────────────

_pool: ConnectionPool | None = None
_client: Redis | None = None


# ── Lifecycle ─────────────────────────────────────────────────────────────────


async def init_redis(settings: Settings) -> None:
    """Create the async Redis connection pool."""
    global _pool, _client

    logger.info("Initialising Redis connection pool…")

    _pool = aioredis.ConnectionPool.from_url(
        settings.redis_url,
        max_connections=settings.redis_max_connections,
        decode_responses=True,
        health_check_interval=30,
    )

    _client = aioredis.Redis(connection_pool=_pool)

    # Verify connectivity
    try:
        pong = await _client.ping()
        if pong:
            logger.info(
                "Redis pool initialised (max_connections=%d)",
                settings.redis_max_connections,
            )
    except RedisError as exc:
        logger.error("Redis ping failed: %s", exc)
        raise


async def close_redis() -> None:
    """Disconnect all pool connections gracefully."""
    global _pool, _client

    if _client is not None:
        logger.info("Closing Redis connection pool…")
        await _client.aclose()
        _client = None

    if _pool is not None:
        await _pool.aclose()
        _pool = None
        logger.info("Redis pool closed.")


# ── Client accessor ───────────────────────────────────────────────────────────


def get_redis() -> Redis:
    """Return the shared Redis client or raise if not initialised."""
    if _client is None:
        raise RuntimeError("Redis not initialised. Call init_redis() first.")
    return _client


# ── Cache helpers ─────────────────────────────────────────────────────────────


async def cache_get(key: str) -> str | None:
    """Get a value from the Redis cache."""
    client = get_redis()
    try:
        return await client.get(key)
    except RedisError as exc:
        logger.warning("Cache GET failed for key '%s': %s", key, exc)
        return None


async def cache_set(key: str, value: str, ttl: int | None = None) -> bool:
    """Set a value in the Redis cache with optional TTL (seconds)."""
    client = get_redis()
    try:
        return bool(await client.set(key, value, ex=ttl))
    except RedisError as exc:
        logger.warning("Cache SET failed for key '%s': %s", key, exc)
        return False


async def cache_delete(key: str) -> int:
    """Delete a key from the Redis cache."""
    client = get_redis()
    try:
        return await client.delete(key)
    except RedisError as exc:
        logger.warning("Cache DELETE failed for key '%s': %s", key, exc)
        return 0


# ── Pub/Sub helpers ───────────────────────────────────────────────────────────


async def publish(channel: str, message: str) -> None:
    """Publish a message to a Redis pub/sub channel."""
    client = get_redis()
    try:
        await client.publish(channel, message)
    except RedisError as exc:
        logger.error("Pub/Sub PUBLISH failed on channel '%s': %s", channel, exc)
        raise


def get_pubsub() -> aioredis.client.PubSub:
    """Return a new PubSub object bound to the shared connection pool."""
    client = get_redis()
    return client.pubsub(ignore_subscribe_messages=True)
