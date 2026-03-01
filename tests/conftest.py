"""
tests/conftest.py — Shared pytest fixtures for the funding-radar test suite.
"""

from __future__ import annotations

import json
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import AsyncClient, ASGITransport

# ── Fake Redis ────────────────────────────────────────────────────────────────

class FakeRedis:
    """Minimal in-memory Redis mock for unit tests."""

    def __init__(self):
        self._store: dict[str, str] = {}
        self._published: list[tuple[str, str]] = []

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value, ex: int | None = None, nx: bool = False) -> bool:
        if nx and key in self._store:
            return False
        self._store[key] = str(value)
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                removed += 1
        return removed

    async def publish(self, channel: str, message: str) -> int:
        self._published.append((channel, message))
        return 1

    def pipeline(self, transaction: bool = True) -> "FakePipeline":
        return FakePipeline(self)

    async def ping(self) -> bool:
        return True

    async def script_load(self, script: str) -> str:
        return "fakeshahex"

    async def evalsha(self, sha: str, numkeys: int, *args) -> list:
        # Always allow — don't test rate limiting in unit tests
        return [1, 9999, 0]


class FakePipeline:
    def __init__(self, redis: FakeRedis):
        self._redis = redis
        self._calls: list = []

    def publish(self, channel: str, message: str):
        self._calls.append(("publish", channel, message))
        return self

    def set(self, key: str, value, ex: int | None = None):
        self._calls.append(("set", key, value))
        return self

    async def execute(self) -> list:
        results = []
        for call in self._calls:
            if call[0] == "publish":
                await self._redis.publish(call[1], call[2])
                results.append(1)
            elif call[0] == "set":
                await self._redis.set(call[1], call[2])
                results.append(True)
        return results


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


# ── FastAPI test client ────────────────────────────────────────────────────────

@pytest.fixture
def app(fake_redis):
    """Create the FastAPI app with Redis mocked out."""
    with patch("app.core.redis.get_redis", return_value=fake_redis), \
         patch("app.core.redis.init_redis", new_callable=AsyncMock), \
         patch("app.core.redis.close_redis", new_callable=AsyncMock), \
         patch("app.core.database.init_db", new_callable=AsyncMock), \
         patch("app.core.database.close_db", new_callable=AsyncMock), \
         patch("app.core.scheduler.init_scheduler", return_value=MagicMock()), \
         patch("app.core.scheduler.shutdown_scheduler"), \
         patch("app.collectors.registry.CollectorRegistry"), \
         patch("app.services.funding_service.FundingService"), \
         patch("app.core.websocket_manager.WebSocketManager"), \
         patch("app.core.redis_ws_bridge.RedisBridge"), \
         patch("app.services.notification_service.NotificationService"), \
         patch("app.bot.telegram_bot.TelegramBotRunner"):
        from app.main import create_app
        application = create_app()
        yield application


@pytest_asyncio.fixture
async def async_client(app) -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
def sync_client(app) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# ── Sample data ───────────────────────────────────────────────────────────────

@pytest.fixture
def hl_meta_and_ctx():
    """Minimal Hyperliquid metaAndAssetCtxs response."""
    return [
        {
            "universe": [
                {"name": "BTC", "szDecimals": 5, "maxLeverage": 50},
                {"name": "ETH", "szDecimals": 4, "maxLeverage": 50},
            ]
        },
        [
            {
                "funding": "0.0001",
                "openInterest": "1000",
                "prevDayPx": "65000",
                "dayNtlVlm": "500000000",
                "oraclePx": "64900",
                "markPx": "65000",
                "midPx": "64990",
                "premium": "0.0002",
            },
            {
                "funding": "0.00005",
                "openInterest": "5000",
                "prevDayPx": "3500",
                "dayNtlVlm": "200000000",
                "oraclePx": "3495",
                "markPx": "3500",
                "midPx": "3498",
                "premium": "0.0001",
            },
        ],
    ]


@pytest.fixture
def aster_ws_snapshot():
    """Minimal Aster WS snapshot + ticker data."""
    ws_snapshots = {
        "BTCUSDT": {
            "mark_price": 65000.0,
            "index_price": 64900.0,
            "funding_rate": 0.0004,
            "next_funding_time": 1700000000000,
        },
        "ETHUSDT": {
            "mark_price": 3500.0,
            "index_price": 3495.0,
            "funding_rate": 0.0001,
            "next_funding_time": 1700000000000,
        },
    }
    ticker_cache = {
        "BTCUSDT": {
            "symbol": "BTCUSDT",
            "openInterest": "100",
            "quoteVolume": "500000000",
        },
        "ETHUSDT": {
            "symbol": "ETHUSDT",
            "openInterest": "2000",
            "quoteVolume": "200000000",
        },
    }
    return ws_snapshots, ticker_cache


@pytest.fixture
def sample_arb_opportunities():
    return [
        {
            "token": "BTC",
            "long_leg": {
                "exchange": "hyperliquid",
                "funding_apr": -10.5,
                "open_interest_usd": 65_000_000,
            },
            "short_leg": {
                "exchange": "aster",
                "funding_apr": 15.2,
                "open_interest_usd": 32_000_000,
            },
            "funding_delta_apr": 25.7,
            "net_apr_taker": 24.5,
            "price_spread_pct": 0.12,
            "breakeven_hours_taker": 3.5,
        },
        {
            "token": "ETH",
            "long_leg": {
                "exchange": "hyperliquid",
                "funding_apr": 2.1,
                "open_interest_usd": 10_000_000,
            },
            "short_leg": {
                "exchange": "aster",
                "funding_apr": 8.4,
                "open_interest_usd": 5_000_000,
            },
            "funding_delta_apr": 6.3,
            "net_apr_taker": 5.8,
            "price_spread_pct": 0.08,
            "breakeven_hours_taker": 12.0,
        },
    ]
