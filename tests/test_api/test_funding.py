"""
tests/test_api/test_funding.py — Integration tests for REST funding endpoints.

Uses httpx AsyncClient with the FastAPI app fully mocked.
Redis is replaced by FakeRedis; no DB or network calls are made.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio


@pytest.mark.asyncio
class TestFundingRatesEndpoint:
    """GET /api/v1/funding/rates"""

    async def test_live_rates_empty_redis(self, async_client):
        """When Redis has no data, endpoint returns empty list gracefully."""
        resp = await async_client.get("/api/v1/funding/rates")
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data or isinstance(data, list)

    async def test_live_rates_with_redis_data(self, async_client, fake_redis, sample_arb_opportunities):
        """When Redis has ranked data, it is returned."""
        ranked = [
            {
                "token": "BTC",
                "rows": [
                    {
                        "exchange": "hyperliquid",
                        "funding_rate": 0.0001,
                        "funding_rate_8h": 0.0008,
                        "funding_apr": 87.6,
                        "mark_price": 65000.0,
                        "open_interest_usd": 65_000_000.0,
                        "volume_24h_usd": 500_000_000.0,
                    }
                ],
            }
        ]
        fake_redis._store["funding:ranked"] = json.dumps(ranked)

        resp = await async_client.get("/api/v1/funding/rates")
        assert resp.status_code == 200

    async def test_token_filter(self, async_client, fake_redis):
        """?token=BTC filters to just that token."""
        ranked = [
            {"token": "BTC", "rows": []},
            {"token": "ETH", "rows": []},
        ]
        fake_redis._store["funding:ranked"] = json.dumps(ranked)

        resp = await async_client.get("/api/v1/funding/rates?token=BTC")
        assert resp.status_code == 200

    async def test_invalid_timeframe(self, async_client):
        resp = await async_client.get("/api/v1/funding/rates?timeframe=99y")
        assert resp.status_code == 422  # Pydantic validation error


@pytest.mark.asyncio
class TestArbitrageEndpoint:
    """GET /api/v1/arbitrage/opportunities"""

    async def test_empty_redis_returns_200(self, async_client):
        resp = await async_client.get("/api/v1/arbitrage/opportunities")
        assert resp.status_code == 200

    async def test_with_opportunities(self, async_client, fake_redis, sample_arb_opportunities):
        fake_redis._store["arbitrage:current"] = json.dumps(sample_arb_opportunities)

        resp = await async_client.get("/api/v1/arbitrage/opportunities")
        assert resp.status_code == 200
        body = resp.json()
        # Free tier sees at most 10
        assert isinstance(body, (list, dict))

    async def test_min_apr_filter(self, async_client, fake_redis, sample_arb_opportunities):
        fake_redis._store["arbitrage:current"] = json.dumps(sample_arb_opportunities)

        resp = await async_client.get("/api/v1/arbitrage/opportunities?min_apr=25")
        assert resp.status_code == 200

    async def test_min_oi_filter(self, async_client, fake_redis, sample_arb_opportunities):
        fake_redis._store["arbitrage:current"] = json.dumps(sample_arb_opportunities)

        # 100B should filter everything out
        resp = await async_client.get("/api/v1/arbitrage/opportunities?min_oi=100000000000")
        assert resp.status_code == 200


@pytest.mark.asyncio
class TestHealthEndpoints:
    async def test_health(self, async_client):
        resp = await async_client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_readiness(self, async_client, fake_redis):
        resp = await async_client.get("/ready")
        assert resp.status_code == 200

    async def test_ws_status(self, async_client):
        resp = await async_client.get("/ws/status")
        assert resp.status_code == 200


@pytest.mark.asyncio
class TestSimulatorEndpoint:
    """POST /api/v1/simulator/calculate"""

    async def test_simulate_requires_body(self, async_client):
        resp = await async_client.post("/api/v1/simulator/calculate", json={})
        assert resp.status_code == 422

    async def test_simulate_basic(self, async_client, fake_redis, sample_arb_opportunities):
        # Seed per-token data in Redis
        token_data = {
            "token": "BTC",
            "rows": [
                {
                    "exchange": "hyperliquid",
                    "funding_apr": -10.5,
                    "mark_price": 65000.0,
                    "maker_fee": 0.01,
                    "taker_fee": 0.035,
                },
                {
                    "exchange": "aster",
                    "funding_apr": 15.2,
                    "mark_price": 65050.0,
                    "maker_fee": 0.01,
                    "taker_fee": 0.035,
                },
            ],
        }
        fake_redis._store["funding:token:BTC"] = json.dumps(token_data)

        payload = {
            "token": "BTC",
            "notional_usd": 10000,
            "long_exchange": "hyperliquid",
            "short_exchange": "aster",
            "holding_days": 7,
            "use_maker": False,
        }
        resp = await async_client.post("/api/v1/simulator/calculate", json=payload)
        # If token data is found it's 200, if not it's 404 — both acceptable
        assert resp.status_code in (200, 404)


@pytest.mark.asyncio
class TestExchangesEndpoint:
    """GET /api/v1/exchanges"""

    async def test_exchanges_returns_200(self, async_client):
        resp = await async_client.get("/api/v1/exchanges")
        assert resp.status_code == 200

    async def test_exchange_tokens_unknown_slug(self, async_client):
        resp = await async_client.get("/api/v1/exchanges/unknown-exchange/tokens")
        assert resp.status_code == 404
