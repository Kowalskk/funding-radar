"""
app/collectors/aster.py — Funding rate collector for Aster DEX (Binance Futures-style API).

Protocol:
  • WebSocket (wss://fstream.asterdex.com)
      - Combined stream "!markPrice@arr" → all mark prices + current funding rate every 3s
  • REST GET /fapi/v1/ticker/24hr  (every 30s)
      - Volume, open interest, 24h price stats for all symbols
  • REST GET /fapi/v1/fundingRate  (every 1h per symbol)
      - Historical funding rate records
  • REST GET /fapi/v1/exchangeInfo (once at startup)
      - Universe of active perpetual symbols

Normalisation rules (Aster specifics):
  • Symbol: "BTCUSDT" → token "BTC"  (strip "USDT" / "BUSD" suffix)
  • lastFundingRate is the *8-hour* rate
      → funding_rate_8h  = lastFundingRate
      → funding_rate     = lastFundingRate          (kept as raw reported rate)
      → funding_apr      = lastFundingRate × 3 × 365 × 100
  • open_interest_usd: /ticker/24hr openInterest is in base-asset units → × markPrice
  • volume_24h_usd: quoteVolume (already in USDT)
  • Funding interval: 8 hours

Rate-limit handling:
  • Respect X-MBX-USED-WEIGHT response header; back off if usage > 80% of the 1200/min limit
  • Separate tracking per minute window

Fees:
  • Maker: 0.01 %  |  Taker: 0.035 %

Docs: Binance Futures API-style (Aster fork)
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed

from app.collectors.base import BaseCollector, CollectorConfig, NormalizedFundingData

# ── Constants ──────────────────────────────────────────────────────────────────

REST_BASE = "https://fapi.asterdex.com"
WS_BASE = "wss://fstream.asterdex.com"
WS_COMBINED_MARK_PRICE = f"{WS_BASE}/stream?streams=!markPrice@arr"

MAKER_FEE = 0.01     # 0.01 %
TAKER_FEE = 0.035    # 0.035 %
FUNDING_INTERVAL_HOURS = 8

# Aster re-uses Binance weight system (1200 weight / min)
WEIGHT_LIMIT_PER_MIN = 1200
WEIGHT_BACKOFF_THRESHOLD = 0.80   # back off when used > 80 %

# Symbol suffixes to strip when producing canonical token name
_QUOTE_SUFFIXES = ("USDT", "BUSD", "USDC", "USD")


def _strip_quote(symbol: str) -> str:
    """'BTCUSDT' → 'BTC', 'ETHBUSD' → 'ETH', 'XRPUSD' → 'XRP'."""
    for suffix in _QUOTE_SUFFIXES:
        if symbol.endswith(suffix):
            return symbol[: -len(suffix)]
    return symbol


class AsterCollector(BaseCollector):
    """Collects live funding rate data from Aster DEX."""

    exchange_slug = "aster"
    funding_interval_hours = FUNDING_INTERVAL_HOURS
    maker_fee = MAKER_FEE
    taker_fee = TAKER_FEE

    def __init__(self, redis_client, config: CollectorConfig | None = None) -> None:
        super().__init__(redis_client, config)

        # Latest WS snapshots: symbol → partial NormalizedFundingData fields
        self._ws_snapshots: dict[str, dict[str, Any]] = {}

        # Ticker data from REST /ticker/24hr: symbol → ticker dict
        self._ticker_cache: dict[str, dict[str, Any]] = {}

        # Active symbols fetched from /exchangeInfo
        self._active_symbols: set[str] = set()

        # Rate-limit weight tracking
        self._current_weight: int = 0
        self._weight_window_start: float = time.monotonic()

        # REST poll interval for ticker is 30s (override base default of 10s)
        self._ticker_poll_interval = 30.0

    # ── Startup override — fetch universe first ────────────────────────────────

    async def start(self) -> None:
        """Fetch exchange universe before starting collection tasks."""
        import aiohttp as _aiohttp
        import json as _json
        # Create the HTTP session early so _load_exchange_info can use _fetch_rest
        if self._session is None or self._session.closed:
            self._session = _aiohttp.ClientSession(
                timeout=_aiohttp.ClientTimeout(total=self._config.http_timeout),
                json_serialize=_json.dumps,
                connector=_aiohttp.TCPConnector(limit=10, ttl_dns_cache=300),
            )
        await self._load_exchange_info()
        # super().start() will not recreate session if already open
        await super().start()

    # ── Exchange info (once at startup) ───────────────────────────────────────

    async def _load_exchange_info(self) -> None:
        """Fetch active perpetual symbols from /fapi/v1/exchangeInfo."""
        try:
            data = await self._fetch_rest(
                f"{REST_BASE}/fapi/v1/exchangeInfo", method="GET"
            )
            symbols: list[dict] = data.get("symbols", [])
            self._active_symbols = {
                s["symbol"]
                for s in symbols
                if s.get("status") == "TRADING"
                and s.get("contractType") == "PERPETUAL"
            }
            self._log.info(
                "Loaded %d active symbols from Aster exchangeInfo.",
                len(self._active_symbols),
            )
        except Exception as exc:
            self._log.warning(
                "Failed to load exchangeInfo; will accept all WS symbols: %s", exc
            )

    # ── REST: ticker + OI polling every 30 s ──────────────────────────────────

    async def _poll_rest(self) -> list[NormalizedFundingData]:
        """Fetch 24h ticker stats, merge with WS snapshots, emit normalised data."""
        try:
            ticker_list = await self._fetch_rest(
                f"{REST_BASE}/fapi/v1/ticker/24hr", method="GET"
            )
            # Re-build ticker cache
            self._ticker_cache = {t["symbol"]: t for t in ticker_list}
        except Exception as exc:
            self._log.error("ticker/24hr fetch failed: %s", exc)
            return []

        # We also need current funding rates (premiumIndex) for symbols not yet
        # seen on the WS (e.g. low-volume assets that emit infrequently)
        try:
            premium_list = await self._fetch_rest(
                f"{REST_BASE}/fapi/v1/premiumIndex", method="GET"
            )
        except Exception as exc:
            self._log.warning("premiumIndex fetch failed: %s", exc)
            premium_list = []

        # Merge premiumIndex into WS snapshot cache (WS takes priority when present)
        for item in premium_list:
            sym = item.get("symbol", "")
            if sym and sym not in self._ws_snapshots:
                self._ws_snapshots[sym] = {
                    "mark_price": float(item.get("markPrice") or 0),
                    "index_price": float(item.get("indexPrice") or 0),
                    "funding_rate": float(item.get("lastFundingRate") or 0),
                    "next_funding_time": item.get("nextFundingTime"),
                }

        return self._build_normalized()

    # ── WebSocket: combined !markPrice@arr stream ──────────────────────────────

    async def _run_ws(self) -> None:
        """Subscribe to the combined markPrice stream for all symbols."""
        async with websockets.connect(
            WS_COMBINED_MARK_PRICE,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
            max_size=2 ** 23,  # 8 MB for large combined frames
        ) as ws:
            self._log.info("Aster WebSocket connected: %s", WS_COMBINED_MARK_PRICE)
            async for raw_msg in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw_msg)
                    self._handle_ws_message(msg)
                except (json.JSONDecodeError, KeyError, TypeError) as exc:
                    self._log.debug(
                        "Unparseable WS message (first 120 chars): %s — %s",
                        str(raw_msg)[:120],
                        exc,
                    )

    def _handle_ws_message(self, msg: dict[str, Any]) -> None:
        """Handle combined stream envelope and individual markPriceUpdate events.

        Combined stream envelope:
            {"stream": "!markPrice@arr", "data": [...]}

        Individual markPriceUpdate:
            {
              "e": "markPriceUpdate",
              "s": "BTCUSDT",
              "p": "11794.15",   ← mark price
              "i": "11784.62",   ← index price
              "r": "0.00038167", ← funding rate (8h)
              "T": 1562306400000 ← next funding time (ms)
            }
        """
        # Unwrap combined stream envelope
        data = msg.get("data", msg)

        if isinstance(data, list):
            for event in data:
                self._process_mark_price_event(event)
        elif isinstance(data, dict):
            self._process_mark_price_event(data)

    def _process_mark_price_event(self, event: dict[str, Any]) -> None:
        if event.get("e") != "markPriceUpdate":
            return

        symbol: str = event.get("s", "")
        if not symbol:
            return
        if self._active_symbols and symbol not in self._active_symbols:
            return  # ignore unknown / non-perpetual symbols

        try:
            self._ws_snapshots[symbol] = {
                "mark_price": float(event.get("p") or 0),
                "index_price": float(event.get("i") or 0),
                "funding_rate": float(event.get("r") or 0),
                "next_funding_time": event.get("T"),  # unix ms or None
            }
        except (TypeError, ValueError) as exc:
            self._log.debug("markPriceUpdate parse error for %s: %s", symbol, exc)

    # ── Historical fetch every 1 h ────────────────────────────────────────────

    async def _fetch_history(self) -> None:
        """Fetch the last 100 funding rate records per symbol and push to Redis."""
        symbols = list(self._active_symbols) or list(self._ws_snapshots.keys())
        if not symbols:
            self._log.debug("No symbols for history fetch; skipping.")
            return

        for symbol in symbols:
            if not self._running:
                break
            try:
                records = await self._fetch_rest(
                    f"{REST_BASE}/fapi/v1/fundingRate?symbol={symbol}&limit=100",
                    method="GET",
                )
                if records:
                    await self._redis.publish(
                        "funding:history",
                        json.dumps(
                            {
                                "exchange": self.exchange_slug,
                                "token": _strip_quote(symbol),
                                "symbol": symbol,
                                "records": records,
                            }
                        ),
                    )
                    self._log.debug(
                        "Published %d history records for %s/%s.",
                        len(records),
                        self.exchange_slug,
                        symbol,
                    )
                # ~100ms between symbols to respect rate limits
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.warning(
                    "History fetch failed for %s/%s: %s", self.exchange_slug, symbol, exc
                )

    # ── Normalisation ──────────────────────────────────────────────────────────

    def _normalize(self, raw_data: Any) -> list[NormalizedFundingData]:
        """Not used directly — normalisation is driven by _build_normalized()."""
        return self._build_normalized()

    def _build_normalized(self) -> list[NormalizedFundingData]:
        """Merge WS snapshot data with REST ticker cache into NormalizedFundingData.

        Called after every REST poll so that volume/OI are always fresh.
        """
        now_ms = self._now_ms()
        results: list[NormalizedFundingData] = []

        for symbol, snap in list(self._ws_snapshots.items()):
            try:
                token = _strip_quote(symbol)
                mark_price = snap.get("mark_price", 0.0)
                index_price = snap.get("index_price", 0.0)
                funding_rate = snap.get("funding_rate", 0.0)
                next_funding_time = snap.get("next_funding_time")

                # Pull volume / OI from ticker cache
                ticker = self._ticker_cache.get(symbol, {})
                # openInterest is in base-asset units → convert to USD
                oi_raw = float(ticker.get("openInterest") or 0)
                open_interest_usd = oi_raw * mark_price if mark_price > 0 else 0.0
                # quoteVolume is already in USD (USDT notional)
                volume_24h_usd = float(ticker.get("quoteVolume") or 0)

                # ── Filters ───────────────────────────────────────────────────
                if open_interest_usd < self._config.min_open_interest_usd:
                    continue
                if volume_24h_usd <= 0:
                    continue

                # ── Derived metrics ───────────────────────────────────────────
                # Aster reports the 8h rate directly
                funding_rate_8h = funding_rate
                funding_apr = self._compute_funding_apr(
                    funding_rate, FUNDING_INTERVAL_HOURS
                )  # rate * (24/8) * 365 * 100 = rate * 3 * 365 * 100

                price_spread_pct = (
                    ((mark_price - index_price) / index_price * 100)
                    if index_price > 0
                    else 0.0
                )

                results.append(
                    NormalizedFundingData(
                        exchange=self.exchange_slug,
                        token=token,
                        symbol=symbol,
                        funding_rate=funding_rate,
                        funding_rate_8h=funding_rate_8h,
                        funding_apr=funding_apr,
                        funding_interval_hours=FUNDING_INTERVAL_HOURS,
                        next_funding_time=(
                            int(next_funding_time) if next_funding_time else None
                        ),
                        predicted_rate=None,
                        mark_price=mark_price,
                        index_price=index_price,
                        open_interest_usd=open_interest_usd,
                        volume_24h_usd=volume_24h_usd,
                        price_spread_pct=price_spread_pct,
                        maker_fee=MAKER_FEE,
                        taker_fee=TAKER_FEE,
                        timestamp=now_ms,
                        is_live=True,  # WS keeps snapshots live
                    )
                )

            except (TypeError, ValueError, KeyError) as exc:
                self._log.warning(
                    "Normalization error for %s symbol %s: %s",
                    self.exchange_slug,
                    symbol,
                    exc,
                )
                continue

        self._log.debug(
            "Normalized %d/%d symbols from Aster.",
            len(results),
            len(self._ws_snapshots),
        )
        return results

    # ── HTTP override — track X-MBX-USED-WEIGHT ───────────────────────────────

    async def _fetch_rest(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: dict | None = None,
        headers: dict | None = None,
    ) -> Any:
        """Override to inspect X-MBX-USED-WEIGHT response header."""
        await self._rate_limit()
        await self._mbx_weight_check()

        import aiohttp as _aiohttp
        from tenacity import (
            before_sleep_log,
            retry,
            retry_if_exception_type,
            stop_after_attempt,
            wait_exponential,
        )
        import logging as _logging

        @retry(
            retry=retry_if_exception_type(
                (_aiohttp.ClientError, asyncio.TimeoutError)
            ),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            stop=stop_after_attempt(self._config.http_max_retries),
            before_sleep=before_sleep_log(self._log, _logging.WARNING),
            reraise=True,
        )
        async def _do() -> Any:
            assert self._session is not None
            async with self._session.request(
                method,
                url,
                json=payload if method == "POST" else None,
                headers=headers or {},
            ) as resp:
                # Track weight consumption
                weight_str = resp.headers.get("X-MBX-USED-WEIGHT-1M", "0")
                try:
                    self._current_weight = int(weight_str)
                    self._log.debug(
                        "Aster API weight used: %d/%d",
                        self._current_weight,
                        WEIGHT_LIMIT_PER_MIN,
                    )
                except ValueError:
                    pass

                resp.raise_for_status()
                return await resp.json()

        return await _do()

    async def _mbx_weight_check(self) -> None:
        """Back off if the current weight exceeds the threshold."""
        now = time.monotonic()
        # Reset window every 60 s
        if now - self._weight_window_start >= 60:
            self._current_weight = 0
            self._weight_window_start = now
            return

        usage_ratio = self._current_weight / WEIGHT_LIMIT_PER_MIN
        if usage_ratio >= WEIGHT_BACKOFF_THRESHOLD:
            wait_s = 60 - (now - self._weight_window_start) + 0.5
            self._log.warning(
                "Aster weight usage %.0f%% — backing off %.1fs",
                usage_ratio * 100,
                wait_s,
            )
            await asyncio.sleep(max(wait_s, 0))
            self._current_weight = 0
            self._weight_window_start = time.monotonic()
