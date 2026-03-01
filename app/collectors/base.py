"""
app/collectors/base.py — Abstract base class for all DEX funding rate collectors.

Each concrete collector must implement `_normalize()` and optionally override
`_poll_rest()` and `_run_ws()` to match the exchange's transport protocol.

Flow per collector:
  start() ──► _run_ws()  (long-lived WebSocket for real-time mid prices)
          └── _poll_loop() (periodic REST for funding rates, OI, volume)

On every data update:
  _normalize(raw) → list[NormalizedFundingData] → _publish() → Redis pub/sub
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any

import aiohttp
from redis.asyncio import Redis
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

# ── Data contract ─────────────────────────────────────────────────────────────


@dataclass
class NormalizedFundingData:
    """Single, normalised funding rate snapshot for one asset on one exchange.

    All numeric fields are plain floats; the layer that writes to TimescaleDB
    converts to Decimal before insertion.
    """

    exchange: str                       # exchange slug, e.g. "hyperliquid"
    token: str                          # canonical symbol, e.g. "BTC"
    symbol: str                         # raw exchange symbol, e.g. "BTC-USD"

    # ── Funding ───────────────────────────────────────────────────────────────
    funding_rate: float                 # raw rate for one interval
    funding_rate_8h: float              # normalised to 8-hour basis
    funding_apr: float                  # annualised (%)
    funding_interval_hours: int         # how often funding settles (1, 4, 8 …)
    next_funding_time: int | None       # unix ms; None if unknown
    predicted_rate: float | None        # exchange-provided prediction; None if n/a

    # ── Market context ────────────────────────────────────────────────────────
    mark_price: float
    index_price: float
    open_interest_usd: float
    volume_24h_usd: float
    price_spread_pct: float             # (mark - index) / index * 100

    # ── Exchange meta ─────────────────────────────────────────────────────────
    maker_fee: float                    # taker/maker as plain %, e.g. 0.035
    taker_fee: float

    # ── Metadata ──────────────────────────────────────────────────────────────
    timestamp: int                      # unix ms when record was captured
    is_live: bool                       # True if sourced from real-time WS

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


# ── Configuration ─────────────────────────────────────────────────────────────


@dataclass
class CollectorConfig:
    """Runtime configuration injected into every collector."""

    rest_poll_interval: float = 10.0        # seconds between REST funding polls
    history_poll_interval: float = 3600.0   # seconds between historical fetches
    ws_reconnect_delay_min: float = 1.0     # minimum back-off (seconds)
    ws_reconnect_delay_max: float = 60.0    # maximum back-off (seconds)
    http_timeout: float = 15.0             # aiohttp total request timeout (s)
    http_max_retries: int = 5              # max retries for transient HTTP errors
    min_open_interest_usd: float = 1_000.0 # filter threshold
    rate_limit_per_minute: int = 1200      # exchange-wide rate limit
    extra: dict[str, Any] = field(default_factory=dict)


# ── Base collector ────────────────────────────────────────────────────────────


class BaseCollector(ABC):
    """Abstract base class that all exchange collectors must inherit from.

    Subclasses *must* implement:
        _normalize(raw_data) -> list[NormalizedFundingData]

    Subclasses *should* override:
        _poll_rest() -> list[NormalizedFundingData]   (called every poll_interval)
        _fetch_history()                              (called every hour)
        _run_ws()                                     (long-lived WebSocket task)
    """

    #: Override in subclass with the exchange slug
    exchange_slug: str = "unknown"
    #: Override with funding interval in hours (1, 4, 8 …)
    funding_interval_hours: int = 8
    maker_fee: float = 0.0
    taker_fee: float = 0.0

    def __init__(
        self,
        redis_client: Redis,
        config: CollectorConfig | None = None,
    ) -> None:
        self._redis = redis_client
        self._config = config or CollectorConfig()
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._session: aiohttp.ClientSession | None = None
        self._log = logging.getLogger(f"collectors.{self.exchange_slug}")

        # Simple token-bucket rate limiter state
        self._request_times: list[float] = []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start all collection tasks (WebSocket + REST polling)."""
        if self._running:
            self._log.warning("Collector already running, ignoring start().")
            return

        self._running = True
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._config.http_timeout),
                json_serialize=json.dumps,
                connector=aiohttp.TCPConnector(limit=10, ttl_dns_cache=300),
            )
        self._log.info("Starting collector for %s.", self.exchange_slug)

        # REST polling task
        self._tasks.append(
            asyncio.create_task(
                self._poll_loop(), name=f"{self.exchange_slug}:rest_poll"
            )
        )
        # WebSocket task
        self._tasks.append(
            asyncio.create_task(
                self._ws_loop(), name=f"{self.exchange_slug}:websocket"
            )
        )
        # Historical data task
        self._tasks.append(
            asyncio.create_task(
                self._history_loop(), name=f"{self.exchange_slug}:history"
            )
        )

    async def stop(self) -> None:
        """Cancel all tasks and close the HTTP session gracefully."""
        if not self._running:
            return

        self._log.info("Stopping collector for %s…", self.exchange_slug)
        self._running = False

        for task in self._tasks:
            task.cancel()

        results = await asyncio.gather(*self._tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                self._log.error(
                    "Task raised on shutdown: %s", result, exc_info=result
                )

        self._tasks.clear()

        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

        self._log.info("Collector %s stopped.", self.exchange_slug)

    # ── Publish ────────────────────────────────────────────────────────────────

    async def _publish(self, data: NormalizedFundingData) -> None:
        """Serialize and publish to the Redis pub/sub channel `funding:updates`."""
        try:
            payload = data.to_json()
            await self._redis.publish("funding:updates", payload)
            # Also cache the latest snapshot per asset
            cache_key = f"funding:latest:{data.exchange}:{data.token}"
            await self._redis.set(
                cache_key, payload, ex=self._config.rest_poll_interval * 3
            )
        except Exception as exc:
            self._log.error("Failed to publish to Redis: %s", exc)

    async def _publish_batch(self, items: list[NormalizedFundingData]) -> None:
        """Publish multiple items via a single Redis pipeline."""
        if not items:
            return
        try:
            pipe = self._redis.pipeline(transaction=False)
            for data in items:
                payload = data.to_json()
                pipe.publish("funding:updates", payload)
                cache_key = f"funding:latest:{data.exchange}:{data.token}"
                pipe.set(cache_key, payload, ex=int(self._config.rest_poll_interval * 3))
            await pipe.execute()
        except Exception as exc:
            self._log.error("Failed batch publish to Redis: %s", exc)

    # ── HTTP helper ────────────────────────────────────────────────────────────

    async def _rate_limit(self) -> None:
        """Enforce the exchange rate limit using a sliding window."""
        now = time.monotonic()
        window = 60.0
        self._request_times = [t for t in self._request_times if now - t < window]
        if len(self._request_times) >= self._config.rate_limit_per_minute:
            sleep_for = window - (now - self._request_times[0]) + 0.05
            self._log.debug("Rate limit reached, sleeping %.2fs", sleep_for)
            await asyncio.sleep(max(sleep_for, 0))
        self._request_times.append(time.monotonic())

    async def _fetch_rest(
        self,
        url: str,
        *,
        method: str = "POST",
        payload: dict | None = None,
        headers: dict | None = None,
    ) -> Any:
        """Make an HTTP request with automatic retry and rate limiting.

        Returns the parsed JSON response body.
        Raises `aiohttp.ClientError` or `asyncio.TimeoutError` after all retries.
        """
        await self._rate_limit()

        @retry(
            retry=retry_if_exception_type(
                (aiohttp.ClientError, asyncio.TimeoutError)
            ),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            stop=stop_after_attempt(self._config.http_max_retries),
            before_sleep=before_sleep_log(self._log, logging.WARNING),
            reraise=True,
        )
        async def _do_request() -> Any:
            assert self._session is not None, "HTTP session not initialised"
            async with self._session.request(
                method,
                url,
                json=payload,
                headers=headers or {},
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

        return await _do_request()

    # ── Internal loops ────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Continuously call `_poll_rest()` every `rest_poll_interval` seconds."""
        self._log.info(
            "REST poll loop started (interval=%.0fs).",
            self._config.rest_poll_interval,
        )
        while self._running:
            start = time.monotonic()
            try:
                items = await self._poll_rest()
                if items:
                    await self._publish_batch(items)
                    self._log.debug(
                        "Published %d assets from %s REST poll.",
                        len(items),
                        self.exchange_slug,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.error(
                    "REST poll error for %s: %s", self.exchange_slug, exc, exc_info=True
                )

            elapsed = time.monotonic() - start
            wait = max(0.0, self._config.rest_poll_interval - elapsed)
            await asyncio.sleep(wait)

    async def _ws_loop(self) -> None:
        """Run the WebSocket with automatic exponential back-off reconnection."""
        delay = self._config.ws_reconnect_delay_min
        while self._running:
            try:
                self._log.info(
                    "Connecting WebSocket for %s…", self.exchange_slug
                )
                await self._run_ws()
                # If _run_ws() returns normally, reset delay
                delay = self._config.ws_reconnect_delay_min
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._running:
                    break
                self._log.warning(
                    "WebSocket error for %s: %s. Reconnecting in %.1fs…",
                    self.exchange_slug,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._config.ws_reconnect_delay_max)

    async def _history_loop(self) -> None:
        """Fetch historical data once at startup and then every hour."""
        # Small initial delay so REST poll can warm up first
        await asyncio.sleep(5)
        while self._running:
            try:
                await self._fetch_history()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.error(
                    "History fetch error for %s: %s",
                    self.exchange_slug,
                    exc,
                    exc_info=True,
                )
            await asyncio.sleep(self._config.history_poll_interval)

    # ── Abstract / overridable hooks ──────────────────────────────────────────

    @abstractmethod
    def _normalize(self, raw_data: Any) -> list[NormalizedFundingData]:
        """Convert raw exchange payload into normalised data objects.

        Must be implemented by every subclass.
        """

    async def _poll_rest(self) -> list[NormalizedFundingData]:
        """Fetch and normalise the current funding snapshot via REST.

        Override in subclass. Default: no-op (exchange may be WS-only).
        """
        return []

    async def _run_ws(self) -> None:
        """Open and maintain the WebSocket connection.

        Override in subclass. Default: sleep forever (REST-only collector).
        """
        self._log.debug(
            "%s has no WebSocket implementation; skipping.", self.exchange_slug
        )
        # Block without doing anything so the ws_loop reconnect logic stays idle
        while self._running:
            await asyncio.sleep(3600)

    async def _fetch_history(self) -> None:
        """Persist historical candles / funding history to TimescaleDB via REST.

        Override in subclass to implement. Default: no-op.
        """

    # ── Utility helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _compute_funding_apr(rate_per_interval: float, interval_hours: int) -> float:
        """Annualise a per-interval funding rate to a % APR."""
        periods_per_year = (24 / interval_hours) * 365
        return rate_per_interval * periods_per_year * 100

    @staticmethod
    def _compute_8h_rate(rate_per_interval: float, interval_hours: int) -> float:
        """Normalise a per-interval rate to the 8-hour equivalent."""
        return rate_per_interval * (8 / interval_hours)
