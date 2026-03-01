"""
tests/test_collectors/test_hyperliquid.py — Unit tests for HyperliquidCollector normalisation.
"""

from __future__ import annotations

import pytest

from app.collectors.base import CollectorConfig
from app.collectors.hyperliquid import HyperliquidCollector


class TestHyperliquidNormalize:
    """Tests for _normalize() — no network calls required."""

    def _make_collector(self, fake_redis) -> HyperliquidCollector:
        cfg = CollectorConfig(min_open_interest_usd=1_000.0)
        c = HyperliquidCollector.__new__(HyperliquidCollector)
        # Manually initialise without calling __init__ to avoid side effects
        c._redis = fake_redis
        c._config = cfg
        c._running = False
        c._tasks = []
        c._session = None
        c._request_times = []
        c._mid_prices = {}
        c._asset_meta = {}
        import logging
        c._log = logging.getLogger("test.hyperliquid")
        return c

    def test_btc_normalization(self, fake_redis, hl_meta_and_ctx):
        collector = self._make_collector(fake_redis)
        results = collector._normalize(hl_meta_and_ctx)

        btc = next((r for r in results if r.token == "BTC"), None)
        assert btc is not None
        assert btc.exchange == "hyperliquid"
        assert btc.funding_interval_hours == 1

        # funding rate is hourly: 0.0001
        # rate_8h = 0.0001 * 8 = 0.0008
        assert btc.funding_rate == pytest.approx(0.0001)
        assert btc.funding_rate_8h == pytest.approx(0.0008)

        # APR = 0.0001 * 24 * 365 * 100 = 87.6%
        assert btc.funding_apr == pytest.approx(87.6, rel=1e-3)

        assert btc.mark_price == pytest.approx(65000.0)
        assert btc.index_price == pytest.approx(64900.0)
        assert btc.open_interest_usd == pytest.approx(65_000_000.0)  # 1000 * 65000
        assert btc.volume_24h_usd == pytest.approx(500_000_000.0)
        assert btc.maker_fee == 0.01
        assert btc.taker_fee == 0.035
        assert btc.is_live is False  # no mid_prices injected

    def test_eth_normalization(self, fake_redis, hl_meta_and_ctx):
        collector = self._make_collector(fake_redis)
        results = collector._normalize(hl_meta_and_ctx)

        eth = next((r for r in results if r.token == "ETH"), None)
        assert eth is not None
        assert eth.funding_rate == pytest.approx(0.00005)
        assert eth.funding_rate_8h == pytest.approx(0.0004)
        assert eth.open_interest_usd == pytest.approx(17_500_000.0)  # 5000 * 3500

    def test_mid_price_override(self, fake_redis, hl_meta_and_ctx):
        collector = self._make_collector(fake_redis)
        collector._mid_prices["BTC"] = 65_100.0  # WS override
        results = collector._normalize(hl_meta_and_ctx)

        btc = next(r for r in results if r.token == "BTC")
        assert btc.mark_price == pytest.approx(65_100.0)
        assert btc.is_live is True

    def test_oi_filter(self, fake_redis):
        """Assets below min OI threshold are excluded."""
        cfg = CollectorConfig(min_open_interest_usd=999_000_000.0)  # 999M — above everything
        collector = self._make_collector(fake_redis)
        collector._config = cfg

        raw = [
            {"universe": [{"name": "BTC", "szDecimals": 5}]},
            [{"funding": "0.0001", "openInterest": "1", "prevDayPx": "100",
              "dayNtlVlm": "1000", "oraclePx": "100", "markPx": "100",
              "midPx": None, "premium": "0"}],
        ]
        results = collector._normalize(raw)
        assert results == []

    def test_volume_filter(self, fake_redis):
        """Assets with zero volume are excluded."""
        collector = self._make_collector(fake_redis)
        raw = [
            {"universe": [{"name": "BTC", "szDecimals": 5}]},
            [{"funding": "0.0001", "openInterest": "1000", "prevDayPx": "65000",
              "dayNtlVlm": "0", "oraclePx": "65000", "markPx": "65000",
              "midPx": None, "premium": "0"}],
        ]
        results = collector._normalize(raw)
        assert results == []

    def test_malformed_input_returns_empty(self, fake_redis):
        collector = self._make_collector(fake_redis)
        assert collector._normalize(None) == []
        assert collector._normalize([]) == []
        assert collector._normalize("garbage") == []

    def test_price_spread_pct(self, fake_redis, hl_meta_and_ctx):
        collector = self._make_collector(fake_redis)
        results = collector._normalize(hl_meta_and_ctx)
        btc = next(r for r in results if r.token == "BTC")
        # (65000 - 64900) / 64900 * 100 ≈ 0.154%
        assert btc.price_spread_pct == pytest.approx(0.1541, rel=1e-2)
