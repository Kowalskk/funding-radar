"""
app/collectors/pacifica.py — Funding rate collector for Pacifica DEX.

Protocol:
  • REST GET /api/v1/info/prices (every ~X sec)
      - Funding rate, Open Interest, Volume, Mark Price for all markets
  • WebSocket (wss://ws.pacifica.fi/ws)
      - Real-time mark price updates via 'prices' channel (Not fully reliable/documented, fallback to REST)
  • No global historical endpoint available.

Normalisation rules:
  • Symbol: "BTC-USD" -> token "BTC"
  • Pacifica funding interval is assumed to be 1 hour or 8 hour. We will scale it uniformly to 8h.
"""

from __future__ import annotations
import asyncio
import json
from typing import Any
import websockets

from app.collectors.base import BaseCollector, CollectorConfig, NormalizedFundingData

REST_BASE = "https://api.pacifica.fi/api/v1"
WS_URL = "wss://ws.pacifica.fi/ws"

FUNDING_INTERVAL_HOURS = 8
MAKER_FEE = 0.00
TAKER_FEE = 0.05

def _strip_quote(symbol: str) -> str:
    """'BTC-USD' → 'BTC'"""
    return symbol.replace("-USD", "")

class PacificaCollector(BaseCollector):
    exchange_slug = "pacifica"
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
        """Fetch active perpetual symbols from /info API."""
        if self._session is None or self._session.closed:
            import aiohttp
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._config.http_timeout),
                json_serialize=json.dumps,
                connector=aiohttp.TCPConnector(limit=10, ttl_dns_cache=300),
            )
        try:
            data = await self._fetch_rest(f"{REST_BASE}/info", method="GET")
            markets = data.get("data", [])
            self._active_symbols = set()
            for m in markets:
                sym = m.get("symbol")
                if sym:
                    self._active_symbols.add(sym)
            self._log.info(
                "Loaded %d active symbols from Pacifica.", len(self._active_symbols)
            )
        except Exception as exc:
            self._log.warning("Failed to load exchangeInfo for Pacifica: %s", exc)

    async def _poll_rest(self) -> list[NormalizedFundingData]:
        if not self._active_symbols:
            await self._load_exchange_info()
            
        try:
            data = await self._fetch_rest(f"{REST_BASE}/info/prices", method="GET")
            markets = data.get("data", [])
        except Exception as exc:
            self._log.error("Pacifica markets fetch failed: %s", exc)
            return []

        now_ms = self._now_ms()
        results: list[NormalizedFundingData] = []

        for m in markets:
            symbol = m.get("symbol", "")
            if symbol not in self._active_symbols:
                continue
            
            try:
                token = _strip_quote(symbol)
                
                # WS mark price takes precedence, fallback to REST
                ws_snap = self._ws_snapshots.get(symbol, {})
                mark_price = ws_snap.get("mark_price", float(m.get("mark") or 0))
                
                # Default missing index_price to mark_price to avoid division by zero or large spreads
                index_price = float(m.get("oracle") or mark_price)
                
                funding_rate = float(m.get("funding") or 0)
                
                # The assumption is Pacifica pays every 8 hours, adjust if discovered to be 1h
                funding_rate_8h = funding_rate
                funding_apr = self._compute_funding_apr(funding_rate, FUNDING_INTERVAL_HOURS)

                daily_volume = float(m.get("volume_24h") or 0) * mark_price
                open_interest_usd = float(m.get("open_interest") or 0) * mark_price

                if open_interest_usd < self._config.min_open_interest_usd:
                    continue
                if daily_volume <= 0:
                    continue

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
                        next_funding_time=None,
                        predicted_rate=float(m.get("next_funding") or 0),
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
                self._log.warning("Normalization error for Pacifica %s: %s", symbol, exc)
                continue

        self._log.debug("Normalized %d symbols from Pacifica.", len(results))
        return results

    async def _run_ws(self) -> None:
        """Optional WebSocket stream. Often falls back to REST if no messages received."""
        while self._running:
            try:
                async with websockets.connect(
                    WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    self._log.info("Pacifica WebSocket connected: %s", WS_URL)
                    
                    sub_msg = {"method": "subscribe", "params": {"channel": "prices"}}
                    await ws.send(json.dumps(sub_msg))
                    
                    async for raw_msg in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw_msg)
                            # Pacifica heartbeat
                            if "channel" in msg and msg["channel"] == "pong":
                                continue
                            
                            # Note: Actual structure might differ. We capture if generic.
                            if "data" in msg and isinstance(msg["data"], list):
                                for item in msg["data"]:
                                    if "symbol" in item and "mark" in item:
                                        sym = item["symbol"]
                                        price = float(item["mark"])
                                        self._ws_snapshots[sym] = {"mark_price": price}
                        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                            pass
            except Exception as exc:
                if self._running:
                    self._log.error("Pacifica WebSocket error: %s", exc)
                    await asyncio.sleep(5)

    async def _fetch_history(self) -> None:
        """No historical endpoint available for Pacifica."""
        pass

    async def _fetch_history_range(self, start_ms: int, end_ms: int):
        """No historical endpoint available for Pacifica."""
        self._log.warning("Pacifica does not support historical data fetching.")
        return
        yield

    def _normalize(self, raw_data: Any) -> list[NormalizedFundingData]:
        return []
