"""
app/api/ws/funding_stream.py — WebSocket endpoint for real-time funding/arb streams.

URL: ws://<host>/ws/funding?token=<jwt>

Client protocol (JSON messages):
─────────────────────────────────
  → { "action": "subscribe",   "channels": ["funding", "arbitrage", "funding:BTC"] }
  → { "action": "unsubscribe", "channels": ["funding:BTC"] }
  → { "type": "pong" }  (heartbeat response — silently accepted)

Server protocol:
─────────────────
  ← { "type": "connected",  "tier": "free", "subscribed": [] }
  ← { "type": "subscribed", "channels": [...], "all_channels": [...] }
  ← { "type": "funding_update",   "channel": "funding",    "data": { ... }, "timestamp": 123 }
  ← { "type": "arbitrage_update", "channel": "arbitrage",  "data": [...],   "timestamp": 123 }
  ← { "type": "token_update",     "channel": "funding:BTC","data": { ... }, "timestamp": 123 }
  ← { "type": "ping",             "timestamp": 123 }
  ← { "type": "error",            "message": "..." }

Authentication:
───────────────
  JWT passed as `?token=<jwt>` query param.
  Missing / invalid token → tier "anonymous" (not a disconnect).
  Expired token → 4001 close code (expired, re-auth required).
"""


import json
import logging
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from jose import ExpiredSignatureError, JWTError, jwt

from app.config import get_settings
from app.core.websocket_manager import WebSocketManager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["WebSocket"])

# ── Valid channels ─────────────────────────────────────────────────────────────

_STATIC_CHANNELS = {"funding", "arbitrage"}


def _is_valid_channel(ch: str) -> bool:
    if ch in _STATIC_CHANNELS:
        return True
    # "funding:{TOKEN}" where TOKEN is 1-12 uppercase letters/digits
    if ch.startswith("funding:"):
        token = ch[len("funding:"):]
        return 1 <= len(token) <= 12 and token.replace("-", "").isalnum()
    return False


# ── JWT helpers ────────────────────────────────────────────────────────────────


def _decode_token(raw: str | None) -> tuple[str, str | None]:
    """Return (tier, user_id_or_None).

    Never raises — bad/missing tokens fall back to 'anonymous'.
    """
    if not raw:
        return "anonymous", None
    settings = get_settings()
    try:
        payload = jwt.decode(
            raw,
            settings.jwt_secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
        )
        tier = payload.get("tier", "free")
        user_id = str(payload.get("sub", ""))
        return tier, user_id
    except ExpiredSignatureError:
        return "__expired__", None
    except JWTError:
        return "anonymous", None


# ── WebSocket endpoint ─────────────────────────────────────────────────────────


@router.websocket("/ws/funding")
async def funding_ws(
    websocket: WebSocket,
    token: str | None = None,  # query param ?token=<jwt>
) -> None:
    """Real-time funding rate / arbitrage stream.

    The manager dependency is pulled from ``app.main`` to avoid a circular
    import (main creates the manager instance).
    """
    from app.main import get_ws_manager

    manager: WebSocketManager = get_ws_manager()

    # ── Auth ──────────────────────────────────────────────────────────────────
    tier, user_id = _decode_token(token)

    if tier == "__expired__":
        await websocket.accept()
        await websocket.close(code=4001, reason="Token expired — please re-authenticate.")
        return

    # ── Connect ──────────────────────────────────────────────────────────────
    info = await manager.connect(websocket, tier)

    try:
        # Greet the client
        await websocket.send_json(
            {
                "type": "connected",
                "tier": tier,
                "user_id": user_id,
                "subscribed": [],
                "timestamp": int(time.time()),
            }
        )

        # ── Message loop ─────────────────────────────────────────────────────
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break

            try:
                msg: dict[str, Any] = json.loads(raw)
            except json.JSONDecodeError:
                await _send_error(websocket, "Invalid JSON.")
                continue

            action = msg.get("action") or msg.get("type", "")

            if action == "subscribe":
                channels: list[str] = msg.get("channels", [])
                valid = [ch for ch in channels if _is_valid_channel(ch)]
                invalid = [ch for ch in channels if not _is_valid_channel(ch)]
                if valid:
                    await manager.subscribe(info, valid)
                await websocket.send_json(
                    {
                        "type": "subscribed",
                        "channels": valid,
                        "all_channels": sorted(info.channels),
                        "invalid": invalid,
                        "timestamp": int(time.time()),
                    }
                )

            elif action == "unsubscribe":
                channels = msg.get("channels", [])
                await manager.unsubscribe(info, channels)
                await websocket.send_json(
                    {
                        "type": "unsubscribed",
                        "channels": channels,
                        "all_channels": sorted(info.channels),
                        "timestamp": int(time.time()),
                    }
                )

            elif action == "pong":
                # Client responded to our ping — nothing to do
                pass

            else:
                await _send_error(websocket, f"Unknown action '{action}'.")

    finally:
        await manager.disconnect(info)


async def _send_error(ws: WebSocket, message: str) -> None:
    try:
        await ws.send_json({"type": "error", "message": message})
    except Exception:
        pass
