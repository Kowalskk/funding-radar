"""
app/collectors/hyperliquid.py — Funding rate collector for Hyperliquid DEX.

Protocol:
  • WebSocket (wss://api.hyperliquid.xyz/ws)
      - Subscribe to "allMids" for real-time mid-price updates
      - Subscribe to "activeAssetCtx" for per-asset context deltas
  • REST POST /info  (every 10s)
      - type "metaAndAssetCtxs" → funding rates, OI, volume, mark/index prices
  • REST POST /info  (every 1h)
      - type "fundingHistory" → per-asset cumulative funding history

Normalisation rules (Hyperliquid specifics):
  • funding_rate is the *hourly* rate → rate_8h = rate * 8
  • funding_apr  = rate * 24 * 365 * 100
  • open_interest_usd = openInterest (in asset units) * mark_price
  • volume_24h_usd = dayNtlVlm (already in USD notional)
  • Funding settles every 1 hour (funding_interval_hours = 1)

Filters applied before publishing:
  • open_interest_usd >= min_open_interest_usd (default $1 000)
  • volume_24h_usd > 0

Docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from app.collectors.base import BaseCollector, CollectorConfig, NormalizedFundingData

# ── Constants ──────────────────────────────────────────────────────────────────

REST_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"

MAKER_FEE = 0.01    # 0.01%
TAKER_FEE = 0.035   # 0.035%
FUNDING_INTERVAL_HOURS = 1

# Ping interval to keep the WebSocket alive (Hyperliquid closes idle sockets ~60s)
WS_PING_INTERVAL = 20  # seconds


class HyperliquidCollector(BaseCollector):
    """Collects live funding rate data from Hyperliquid."""

    exchange_slug = "hyperliquid"
    funding_interval_hours = FUNDING_INTERVAL_HOURS
    maker_fee = MAKER_FEE
    taker_fee = TAKER_FEE

    def __init__(self, redis_client, config: CollectorConfig | None = None) -> None:
        super().__init__(redis_client, config)
        # Shared mid-price map written by the WS and consumed by _poll_rest
        self._mid_prices: dict[str, float] = {}
        # Universe metadata keyed by asset name (from /info?type=meta)
        self._asset_meta: dict[str, dict[str, Any]] = {}

    # ── REST: full snapshot every 10 seconds ──────────────────────────────────

    async def _poll_rest(self) -> list[NormalizedFundingData]:
        """Fetch current funding rates, OI, and volume for all assets."""
        try:
            raw = await self._fetch_rest(
                REST_URL,
                method="POST",
                payload={"type": "metaAndAssetCtxs"},
            )
            return self._normalize(raw)
        except Exception as exc:
            self._log.error("metaAndAssetCtxs fetch failed: %s", exc)
            return []

    # ── REST: historical funding every 1 hour ─────────────────────────────────

    async def _fetch_history(self) -> None:
        """Fetch per-asset funding history for the last 24 h and persist via Redis."""
        if not self._asset_meta:
            self._log.debug("Asset meta not ready; skipping history fetch.")
            return

        now_ms = self._now_ms()
        start_ms = now_ms - 24 * 3_600_000  # last 24 hours

        for asset_name in list(self._asset_meta.keys()):
            if not self._running:
                break
            try:
                raw = await self._fetch_rest(
                    REST_URL,
                    method="POST",
                    payload={
                        "type": "fundingHistory",
                        "coin": asset_name,
                        "startTime": start_ms,
                    },
                )
                if raw:
                    # Publish to a separate channel for DB persistence
                    await self._redis.publish(
                        "funding:history",
                        json.dumps(
                            {
                                "exchange": self.exchange_slug,
                                "token": asset_name,
                                "records": raw,
                            }
                        ),
                    )
                    self._log.debug(
                        "Published %d history records for %s/%s.",
                        len(raw),
                        self.exchange_slug,
                        asset_name,
                    )
                # Respect rate limits: ~1 req per second for history
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.warning(
                    "History fetch failed for %s: %s", asset_name, exc
                )

    # ── WebSocket: real-time mid prices ───────────────────────────────────────

    async def _run_ws(self) -> None:
        """Connect to Hyperliquid WebSocket and stream mid-price updates."""
        async with websockets.connect(
            WS_URL,
            ping_interval=WS_PING_INTERVAL,
            ping_timeout=10,
            close_timeout=5,
            max_size=2**22,  # 4 MB
        ) as ws:
            self._log.info("WebSocket connected: %s", WS_URL)

            # Subscribe to allMids (mid-price feed for all assets)
            await ws.send(
                json.dumps(
                    {"method": "subscribe", "subscription": {"type": "allMids"}}
                )
            )

            async for raw_msg in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw_msg)
                    self._handle_ws_message(msg)
                except (json.JSONDecodeError, KeyError) as exc:
                    self._log.debug("Unparseable WS message: %s — %s", raw_msg[:120], exc)

    def _handle_ws_message(self, msg: dict[str, Any]) -> None:
        """Dispatch incoming WebSocket frames to the appropriate handler."""
        channel = msg.get("channel")
        data = msg.get("data")

        if channel == "allMids" and isinstance(data, dict):
            mids = data.get("mids", {})
            for asset, mid_str in mids.items():
                try:
                    self._mid_prices[asset] = float(mid_str)
                except (TypeError, ValueError):
                    pass

    # ── Normalisation ──────────────────────────────────────────────────────────

    def _normalize(self, raw_data: Any) -> list[NormalizedFundingData]:
        """Convert `metaAndAssetCtxs` response into NormalizedFundingData objects.

        The response is a 2-element list:
          [0] = meta: {"universe": [{"name": str, "szDecimals": int, ...}, ...]}
          [1] = asset_ctxs: [
                  {
                    "funding": str,        # hourly rate as string float
                    "openInterest": str,   # in asset units
                    "prevDayPx": str,      # prev-day mark price
                    "dayNtlVlm": str,      # 24h notional volume USD
                    "premium": str,        # mark - index spread
                    "oraclePx": str,       # index / oracle price
                    "markPx": str,         # current mark price
                    "midPx": str | None,
                    "impactPxs": [...],
                  }, ...
                ]
        """
        if not isinstance(raw_data, list) or len(raw_data) < 2:
            self._log.warning("Unexpected metaAndAssetCtxs shape: %s", type(raw_data))
            return []

        universe: list[dict] = raw_data[0].get("universe", [])
        asset_ctxs: list[dict] = raw_data[1]

        if len(universe) != len(asset_ctxs):
            self._log.warning(
                "Universe (%d) and assetCtxs (%d) length mismatch.",
                len(universe),
                len(asset_ctxs),
            )

        normalized: list[NormalizedFundingData] = []
        now_ms = self._now_ms()

        for meta, ctx in zip(universe, asset_ctxs):
            try:
                asset_name: str = meta.get("name", "")
                if not asset_name:
                    continue

                # Update shared meta cache
                self._asset_meta[asset_name] = meta

                funding_rate = float(ctx.get("funding") or 0)
                mark_price = float(ctx.get("markPx") or ctx.get("prevDayPx") or 0)
                index_price = float(ctx.get("oraclePx") or mark_price)
                open_interest_raw = float(ctx.get("openInterest") or 0)
                volume_24h = float(ctx.get("dayNtlVlm") or 0)

                # Convert OI from asset units to USD
                open_interest_usd = open_interest_raw * mark_price

                # Apply mid-price from WS if available (more up-to-date)
                if asset_name in self._mid_prices:
                    mark_price = self._mid_prices[asset_name]

                # ── Filters ───────────────────────────────────────────────────
                if open_interest_usd < self._config.min_open_interest_usd:
                    continue
                if volume_24h <= 0:
                    continue

                # ── Derived metrics ───────────────────────────────────────────
                funding_rate_8h = self._compute_8h_rate(
                    funding_rate, FUNDING_INTERVAL_HOURS
                )
                funding_apr = self._compute_funding_apr(
                    funding_rate, FUNDING_INTERVAL_HOURS
                )

                price_spread_pct = (
                    ((mark_price - index_price) / index_price * 100)
                    if index_price > 0
                    else 0.0
                )

                # Hyperliquid: premium field = (mark - oracle) / oracle
                # We compute from prices directly for consistency.

                normalized.append(
                    NormalizedFundingData(
                        exchange=self.exchange_slug,
                        token=asset_name,        # "BTC", "ETH", etc.
                        symbol=asset_name,       # Hyperliquid uses name as symbol
                        funding_rate=funding_rate,
                        funding_rate_8h=funding_rate_8h,
                        funding_apr=funding_apr,
                        funding_interval_hours=FUNDING_INTERVAL_HOURS,
                        next_funding_time=None,  # Hyperliquid settles every hour on the hour
                        predicted_rate=None,     # Not provided by this endpoint
                        mark_price=mark_price,
                        index_price=index_price,
                        open_interest_usd=open_interest_usd,
                        volume_24h_usd=volume_24h,
                        price_spread_pct=price_spread_pct,
                        maker_fee=MAKER_FEE,
                        taker_fee=TAKER_FEE,
                        timestamp=now_ms,
                        is_live=asset_name in self._mid_prices,
                    )
                )

            except (TypeError, ValueError, KeyError) as exc:
                self._log.warning(
                    "Normalization error for asset %s: %s", meta.get("name"), exc
                )
                continue

        self._log.debug(
            "Normalized %d/%d assets from Hyperliquid.",
            len(normalized),
            len(universe),
        )
        return normalized
