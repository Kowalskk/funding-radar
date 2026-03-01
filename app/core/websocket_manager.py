"""
app/core/websocket_manager.py — Manages all active WebSocket connections.

Architecture
────────────
  Client ──── /ws/funding?token=<jwt> ──── WebSocketEndpoint
                                                │
                                   WebSocketManager.connect(ws, channels, tier)
                                                │
                     ┌──────────────────────────┼──────────────────────────┐
                     ▼                          ▼                          ▼
              channel "funding"        channel "arbitrage"    channel "funding:BTC"
              {ConnectionInfo, ...}    {ConnectionInfo, ...}  {ConnectionInfo, ...}

  RedisBridge (background task)
    subscribes → "funding:updates", "arbitrage:updates", "funding:ranked:updates"
    calls WebSocketManager.broadcast(channel, payload)

Rate-limiting per tier
──────────────────────
  anonymous / free: min 60s between updates per client
  pro / custom    : min 10s between updates per client
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

logger = logging.getLogger(__name__)

# ── Tier throttle config ──────────────────────────────────────────────────────

_THROTTLE_SECONDS: dict[str, float] = {
    "anonymous": 60.0,
    "free":      60.0,
    "pro":       10.0,
    "custom":    1.0,
}

_HEARTBEAT_INTERVAL = 30.0  # seconds between server-initiated pings


# ── Connection state ──────────────────────────────────────────────────────────


@dataclass
class ConnectionInfo:
    """All state associated with one active WebSocket client."""

    ws: WebSocket
    tier: str
    channels: set[str] = field(default_factory=set)
    last_sent: dict[str, float] = field(default_factory=dict)  # channel → monotonic
    connected_at: float = field(default_factory=time.monotonic)


class WebSocketManager:
    """Central registry for active WebSocket connections.

    Usage (in endpoint):
        info = await manager.connect(ws, tier)
        try:
            await manager.handle_client(info)  # blocks until disconnect
        finally:
            await manager.disconnect(info)
    """

    def __init__(self) -> None:
        # channel → set of ConnectionInfo
        self._channels: dict[str, set[ConnectionInfo]] = {}
        # ws identity → ConnectionInfo (for O(1) disconnect lookup)
        self._connections: dict[int, ConnectionInfo] = {}
        self._lock = asyncio.Lock()
        self._heartbeat_task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the heartbeat background task."""
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name="ws_manager:heartbeat"
            )
            logger.info("WebSocketManager started (heartbeat every %.0fs).", _HEARTBEAT_INTERVAL)

    async def stop(self) -> None:
        """Gracefully close all connections and stop the heartbeat."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        # Close all live connections
        async with self._lock:
            infos = list(self._connections.values())

        close_tasks = [self._close_connection(info, code=1001, reason="Server shutting down")
                       for info in infos]
        await asyncio.gather(*close_tasks, return_exceptions=True)
        logger.info("WebSocketManager stopped (%d connections closed).", len(infos))

    # ── Connection management ─────────────────────────────────────────────────

    async def connect(self, ws: WebSocket, tier: str) -> ConnectionInfo:
        """Accept the WebSocket and register a new ConnectionInfo."""
        await ws.accept()
        info = ConnectionInfo(ws=ws, tier=tier)
        async with self._lock:
            self._connections[id(ws)] = info
        logger.debug("WS connected: tier=%s total=%d", tier, len(self._connections))
        return info

    async def disconnect(self, info: ConnectionInfo) -> None:
        """Unregister a connection from all channels it was subscribed to."""
        async with self._lock:
            self._connections.pop(id(info.ws), None)
            for ch in list(info.channels):
                self._channels.get(ch, set()).discard(info)
                if ch in self._channels and not self._channels[ch]:
                    del self._channels[ch]
        logger.debug(
            "WS disconnected: tier=%s remaining=%d", info.tier, len(self._connections)
        )

    async def subscribe(self, info: ConnectionInfo, channels: list[str]) -> None:
        """Add the client to the requested channels."""
        async with self._lock:
            for ch in channels:
                if ch not in self._channels:
                    self._channels[ch] = set()
                self._channels[ch].add(info)
                info.channels.add(ch)
        logger.debug("WS subscribe: tier=%s channels=%s", info.tier, channels)

    async def unsubscribe(self, info: ConnectionInfo, channels: list[str]) -> None:
        """Remove the client from the specified channels."""
        async with self._lock:
            for ch in channels:
                self._channels.get(ch, set()).discard(info)
                info.channels.discard(ch)
                if ch in self._channels and not self._channels[ch]:
                    del self._channels[ch]

    # ── Broadcasting ──────────────────────────────────────────────────────────

    async def broadcast(self, channel: str, payload: dict | str) -> None:
        """Send ``payload`` to every subscriber of ``channel``, respecting tier throttles.

        Args:
            channel: One of "funding", "arbitrage", "funding:{TOKEN}", …
            payload: Dict (auto JSON-serialised) or pre-serialised JSON string.
        """
        async with self._lock:
            subscribers = set(self._channels.get(channel, set()))  # snapshot

        if not subscribers:
            return

        if isinstance(payload, dict):
            text = json.dumps(payload)
        else:
            text = payload

        now = time.monotonic()
        dead: list[ConnectionInfo] = []

        send_coros = []
        for info in subscribers:
            throttle = _THROTTLE_SECONDS.get(info.tier, 60.0)
            last = info.last_sent.get(channel, 0.0)
            if now - last < throttle:
                continue  # throttled — skip this tick for this client
            info.last_sent[channel] = now
            send_coros.append((info, self._safe_send(info, text)))

        if not send_coros:
            return

        results = await asyncio.gather(*(coro for _, coro in send_coros), return_exceptions=True)
        for (info, _), result in zip(send_coros, results):
            if isinstance(result, Exception):
                dead.append(info)

        # Clean up dead connections outside gather
        for info in dead:
            await self.disconnect(info)

    async def _safe_send(self, info: ConnectionInfo, text: str) -> None:
        """Send text to one client; raises on failure."""
        if info.ws.client_state != WebSocketState.CONNECTED:
            raise WebSocketDisconnect(code=1006)
        await info.ws.send_text(text)

    # ── Heartbeat ─────────────────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Periodically ping all connected clients to detect dead connections."""
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            async with self._lock:
                infos = list(self._connections.values())

            dead: list[ConnectionInfo] = []
            for info in infos:
                try:
                    if info.ws.client_state == WebSocketState.CONNECTED:
                        await info.ws.send_json({"type": "ping", "timestamp": int(time.time())})
                    else:
                        dead.append(info)
                except Exception:
                    dead.append(info)

            for info in dead:
                await self.disconnect(info)

            if dead:
                logger.debug("Heartbeat removed %d dead WS connections.", len(dead))

    @staticmethod
    async def _close_connection(
        info: ConnectionInfo, *, code: int = 1000, reason: str = ""
    ) -> None:
        try:
            if info.ws.client_state == WebSocketState.CONNECTED:
                await info.ws.close(code=code, reason=reason)
        except Exception:
            pass

    # ── Stats ─────────────────────────────────────────────────────────────────

    @property
    def stats(self) -> dict:
        return {
            "total_connections": len(self._connections),
            "channels": {ch: len(subs) for ch, subs in self._channels.items()},
            "heartbeat_alive": (
                self._heartbeat_task is not None and not self._heartbeat_task.done()
            ),
        }
