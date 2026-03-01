"""
app/core/redis_ws_bridge.py — Bridges Redis pub/sub into WebSocket broadcasts.

Subscribes to Redis channels and calls WebSocketManager.broadcast() so that
all connected WS clients receive live updates.

Redis channel → WS channel mapping
────────────────────────────────────
  "funding:updates"         → "funding"  +  "funding:{token}" per-token fan-out
  "arbitrage:updates"       → "arbitrage"
  "funding:ranked:updates"  → "funding"  (full ranked list snapshot)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from redis.asyncio import Redis

from app.core.websocket_manager import WebSocketManager

logger = logging.getLogger(__name__)

# Redis channels to subscribe to
_REDIS_CHANNELS = [
    "funding:updates",
    "arbitrage:updates",
    "funding:ranked:updates",
]


class RedisBridge:
    """Background service that reads from Redis pub/sub and
    fans out to the WebSocketManager.

    One instance per application process.
    """

    def __init__(self, redis: Redis, manager: WebSocketManager) -> None:
        self._redis = redis
        self._manager = manager
        self._task: asyncio.Task | None = None
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._bridge_loop(), name="redis_ws_bridge")
        logger.info("RedisBridge started — subscribing to: %s", _REDIS_CHANNELS)

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("RedisBridge stopped.")

    # ── Bridge loop (auto-reconnects on Redis errors) ─────────────────────────

    async def _bridge_loop(self) -> None:
        while self._running:
            pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
            try:
                await pubsub.subscribe(*_REDIS_CHANNELS)
                logger.debug("RedisBridge pubsub subscribed.")

                async for message in pubsub.listen():
                    if not self._running:
                        break
                    if not message or message.get("type") != "message":
                        continue

                    redis_channel: str = message.get("channel", b"")
                    if isinstance(redis_channel, bytes):
                        redis_channel = redis_channel.decode()

                    raw_data: str | bytes = message.get("data", "{}")
                    if isinstance(raw_data, bytes):
                        raw_data = raw_data.decode()

                    await self._dispatch(redis_channel, raw_data)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._running:
                    break
                logger.error(
                    "RedisBridge error (reconnecting in 3s): %s", exc, exc_info=True
                )
                await asyncio.sleep(3)
            finally:
                try:
                    await pubsub.unsubscribe(*_REDIS_CHANNELS)
                    await pubsub.aclose()
                except Exception:
                    pass

    # ── Dispatch ──────────────────────────────────────────────────────────────

    async def _dispatch(self, redis_channel: str, raw: str) -> None:
        """Route a Redis message to the appropriate WS channel(s)."""
        now = int(time.time())
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("RedisBridge: invalid JSON on %s", redis_channel)
            return

        if redis_channel == "funding:updates":
            await self._dispatch_funding_update(data, now)

        elif redis_channel == "arbitrage:updates":
            payload = json.dumps(
                {
                    "type": "arbitrage_update",
                    "channel": "arbitrage",
                    "data": data,
                    "timestamp": now,
                }
            )
            await self._manager.broadcast("arbitrage", payload)

        elif redis_channel == "funding:ranked:updates":
            # Full ranked list → broadcast to "funding" channel
            payload = json.dumps(
                {
                    "type": "funding_ranked",
                    "channel": "funding",
                    "data": data,
                    "timestamp": now,
                }
            )
            await self._manager.broadcast("funding", payload)

    async def _dispatch_funding_update(self, data: dict, now: int) -> None:
        """Fan-out a single NormalizedFundingData to the general and token channels."""
        token: str = data.get("token", "")

        # General "funding" channel — every funding update
        general_payload = json.dumps(
            {
                "type": "funding_update",
                "channel": "funding",
                "data": data,
                "timestamp": now,
            }
        )
        await self._manager.broadcast("funding", general_payload)

        # Per-token channel "funding:BTC", "funding:ETH", …
        if token:
            token_channel = f"funding:{token.upper()}"
            token_payload = json.dumps(
                {
                    "type": "token_update",
                    "channel": token_channel,
                    "data": data,
                    "timestamp": now,
                }
            )
            await self._manager.broadcast(token_channel, token_payload)
