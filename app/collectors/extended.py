"""
app/collectors/extended.py — Funding rate collector for Extended DEX (Starknet).

Protocol:
  • WebSocket (wss://api.starknet.extended.exchange/stream.extended.exchange/v1/prices/mark)
      - Real-time mark price updates: {"type":"MP","data":{"m":"BTC-USD","p":"25670","ts":1701563440000}}
  • REST GET /api/v1/info/markets  (every ~10s)
      - Funding rate, Open Interest, Volume, Index Price, etc for all markets

Normalisation rules:
  • Symbol: "BTC-USD" -> token "BTC"
  • Extended funding interval is 1 hour
  • funding_rate_8h = rate * 8
  • funding_apr = rate * 24 * 365 * 100
  • volume_24h_usd = dailyVolume
  • open_interest_usd = openInterest
"""

from __future__ import annotations
import asyncio
import json
import time
from typing import Any
import websockets
from websockets.exceptions import ConnectionClosed

from app.collectors.base import BaseCollector, CollectorConfig, NormalizedFundingData

REST_BASE = "https://api.starknet.extended.exchange/api/v1"
WS_MARK_PRICE = "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/prices/mark"

FUNDING_INTERVAL_HOURS = 1
# Assuming standard 0% maker, 0.05% taker for now
MAKER_FEE = 0.00
TAKER_FEE = 0.05

def _strip_quote(symbol: str) -> str:
    """'BTC-USD' → 'BTC'"""
    return symbol.replace("-USD", "")

class ExtendedCollector(BaseCollector):
    exchange_slug = "extended"
    funding_interval_hours = FUNDING_INTERVAL_HOURS
    maker_fee = MAKER_FEE
    taker_fee = TAKER_FEE

    def __init__(self, redis_client, config: CollectorConfig | None = None) -> None:
        super().__init__(redis_client, config)
        self._ws_snapshots: dict[str, dict[str, Any]] = {}
        self._active_symbols: set[str] = set()

    async def start(self) -> None:
        await self._load_exchange_info()
        await super().start()

    async def _load_exchange_info(self) -> None:
        """Fetch active perpetual symbols from /info/markets."""
        if self._session is None or self._session.closed:
            import aiohttp
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._config.http_timeout),
                json_serialize=json.dumps,
                connector=aiohttp.TCPConnector(limit=10, ttl_dns_cache=300),
            )
        try:
            data = await self._fetch_rest(f"{REST_BASE}/info/markets", method="GET")
            markets = data.get("data", [])
            self._active_symbols = set()
            for m in markets:
                if m.get("status") == "ACTIVE" and "USD" in m.get("name", ""):
                    self._active_symbols.add(m["name"])
            self._log.info(
                "Loaded %d active symbols from Extended.", len(self._active_symbols)
            )
        except Exception as exc:
            self._log.warning("Failed to load exchangeInfo for Extended: %s", exc)

    async def _poll_rest(self) -> list[NormalizedFundingData]:
        try:
            data = await self._fetch_rest(f"{REST_BASE}/info/markets", method="GET")
            markets = data.get("data", [])
        except Exception as exc:
            self._log.error("Extended markets fetch failed: %s", exc)
            return []

        now_ms = self._now_ms()
        results: list[NormalizedFundingData] = []

        for m in markets:
            symbol = m.get("name", "")
            if symbol not in self._active_symbols:
                continue
            
            stats = m.get("marketStats", {})
            try:
                token = _strip_quote(symbol)
                # WS mark price takes precedence, fallback to REST
                ws_snap = self._ws_snapshots.get(symbol, {})
                mark_price = ws_snap.get("mark_price", float(stats.get("markPrice") or 0))
                
                index_price = float(stats.get("indexPrice") or 0)
                # Funding rate on Extended is 1-hour
                funding_rate = float(stats.get("fundingRate") or 0)
                next_funding_time = int(stats.get("nextFundingRate") or 0)

                daily_volume = float(stats.get("dailyVolume") or 0)
                open_interest_base = float(stats.get("openInterestBase") or 0)
                open_interest_usd = open_interest_base * mark_price

                if open_interest_usd < self._config.min_open_interest_usd:
                    continue
                if daily_volume <= 0:
                    continue

                funding_rate_8h = funding_rate * 8
                funding_apr = self._compute_funding_apr(funding_rate, FUNDING_INTERVAL_HOURS)

                price_spread_pct = (
                    ((mark_price - index_price) / index_price * 100)
                    if index_price > 0 else 0.0
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
                        next_funding_time=next_funding_time if next_funding_time else None,
                        predicted_rate=None,
                        mark_price=mark_price,
                        index_price=index_price,
                        open_interest_usd=open_interest_usd,
                        volume_24h_usd=daily_volume,
                        price_spread_pct=price_spread_pct,
                        maker_fee=MAKER_FEE,
                        taker_fee=TAKER_FEE,
                        timestamp=now_ms,
                        is_live=symbol in self._ws_snapshots,
                    )
                )

            except (TypeError, ValueError, KeyError) as exc:
                self._log.warning("Normalization error for Extended %s: %s", symbol, exc)
                continue

        self._log.debug("Normalized %d symbols from Extended.", len(results))
        return results

    async def _run_ws(self) -> None:
        async with websockets.connect(
            WS_MARK_PRICE,
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            self._log.info("Extended WebSocket connected: %s", WS_MARK_PRICE)
            async for raw_msg in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw_msg)
                    if msg.get("type") == "MP":
                        data = msg.get("data", {})
                        symbol = data.get("m")
                        price = float(data.get("p") or 0)
                        if symbol:
                            self._ws_snapshots[symbol] = {"mark_price": price}
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    pass

    async def _fetch_history(self) -> None:
        """Fetch recent history routinely to keep up if we fell behind slightly."""
        now_ms = self._now_ms()
        start_ms = now_ms - (2 * 60 * 60 * 1000) # last 2 hours
        PAGE_SIZE = 1000
        
        for symbol in list(self._active_symbols):
            if not self._running:
                break
            try:
                url = (
                    f"{REST_BASE}/info/{symbol}/funding"
                    f"?startTime={start_ms}&endTime={now_ms}&limit={PAGE_SIZE}"
                )
                data = await self._fetch_rest(url, method="GET")
                records = data.get("data", [])
                
                if records:
                    await self._redis.publish(
                        "funding:history",
                        json.dumps({
                            "exchange": self.exchange_slug,
                            "token": _strip_quote(symbol),
                            "symbol": symbol,
                            "records": records,
                        })
                    )
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.warning("History fetch failed for Extended %s: %s", symbol, exc)

    async def _fetch_history_range(self, start_ms: int, end_ms: int):
        if not self._active_symbols:
            await self._load_exchange_info()
            
        PAGE_SIZE = 10000
        now_ms = self._now_ms()

        for symbol in list(self._active_symbols):
            token = _strip_quote(symbol)
            chunk_end = min(end_ms, now_ms)
            
            # The API returns data in descending order of time (newest first).
            # We must iterate backwards from chunk_end to start_ms.
            while chunk_end > start_ms:
                try:
                    url = (
                        f"{REST_BASE}/info/{symbol}/funding"
                        f"?startTime={start_ms}&endTime={chunk_end}&limit={PAGE_SIZE}"
                    )
                    response = await self._fetch_rest(url, method="GET")
                    if not response:
                        break
                    
                    raw_data = response.get("data", [])
                    if not raw_data:
                        break
                        
                    for rec in raw_data:
                        try:
                            rate = float(rec.get("f") or 0)
                            ts = int(rec.get("T") or 0)
                            if not ts:
                                continue
                            
                            yield NormalizedFundingData(
                                exchange=self.exchange_slug,
                                token=token,
                                symbol=symbol,
                                funding_rate=rate,
                                funding_rate_8h=rate * 8,
                                funding_apr=self._compute_funding_apr(rate, FUNDING_INTERVAL_HOURS),
                                funding_interval_hours=FUNDING_INTERVAL_HOURS,
                                next_funding_time=None,
                                predicted_rate=None,
                                mark_price=0.0,
                                index_price=0.0,
                                open_interest_usd=0.0,
                                volume_24h_usd=0.0,
                                price_spread_pct=0.0,
                                maker_fee=MAKER_FEE,
                                taker_fee=TAKER_FEE,
                                timestamp=ts,
                                is_live=False,
                            )
                        except (TypeError, ValueError):
                            continue
                            
                    last_ts = int(raw_data[-1].get("T") or 0)
                    if len(raw_data) < PAGE_SIZE or last_ts <= start_ms:
                        break
                    
                    chunk_end = last_ts - 1
                    await asyncio.sleep(0.1)

                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._log.warning("Extended history range error for %s: %s", symbol, exc)
                    break

    def _normalize(self, raw_data: Any) -> list[NormalizedFundingData]:
        return []
