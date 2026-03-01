"""
tests/test_collectors/test_aster.py — Unit tests for AsterCollector normalisation.
"""

from __future__ import annotations

import pytest

from app.collectors.aster import AsterCollector, _strip_quote
from app.collectors.base import CollectorConfig


class TestStripQuote:
    def test_usdt(self):
        assert _strip_quote("BTCUSDT") == "BTC"

    def test_busd(self):
        assert _strip_quote("ETHBUSD") == "ETH"

    def test_usdc(self):
        assert _strip_quote("SOLUSDC") == "SOL"

    def test_no_suffix(self):
        assert _strip_quote("UNKNOWN") == "UNKNOWN"

    def test_empty(self):
        assert _strip_quote("") == ""


class TestAsterNormalize:
    def _make_collector(self, fake_redis, ws_snapshots, ticker_cache) -> AsterCollector:
        cfg = CollectorConfig(min_open_interest_usd=1_000.0)
        c = AsterCollector.__new__(AsterCollector)
        c._redis = fake_redis
        c._config = cfg
        c._running = False
        c._tasks = []
        c._session = None
        c._request_times = []
        c._ws_snapshots = ws_snapshots
        c._ticker_cache = ticker_cache
        c._active_symbols = set(ws_snapshots.keys())
        c._current_weight = 0
        c._weight_window_start = 0
        c._ticker_poll_interval = 30.0
        import logging
        c._log = logging.getLogger("test.aster")
        return c

    def test_btc_normalization(self, fake_redis, aster_ws_snapshot):
        ws, ticker = aster_ws_snapshot
        collector = self._make_collector(fake_redis, ws, ticker)
        results = collector._build_normalized()

        btc = next((r for r in results if r.token == "BTC"), None)
        assert btc is not None
        assert btc.exchange == "aster"
        assert btc.symbol == "BTCUSDT"
        assert btc.funding_interval_hours == 8

        # Aster reports 8h rate directly: 0.0004
        assert btc.funding_rate == pytest.approx(0.0004)
        assert btc.funding_rate_8h == pytest.approx(0.0004)

        # APR = 0.0004 * 3 * 365 * 100 = 43.8%
        assert btc.funding_apr == pytest.approx(43.8, rel=1e-3)

        # OI: 100 (base units) × 65000 (mark price) = 6,500,000
        assert btc.open_interest_usd == pytest.approx(6_500_000.0)
        assert btc.volume_24h_usd == pytest.approx(500_000_000.0)
        assert btc.maker_fee == 0.01
        assert btc.taker_fee == 0.035
        assert btc.is_live is True

    def test_eth_normalization(self, fake_redis, aster_ws_snapshot):
        ws, ticker = aster_ws_snapshot
        collector = self._make_collector(fake_redis, ws, ticker)
        results = collector._build_normalized()

        eth = next((r for r in results if r.token == "ETH"), None)
        assert eth is not None
        assert eth.funding_rate == pytest.approx(0.0001)
        # APR = 0.0001 * 3 * 365 * 100 = 10.95%
        assert eth.funding_apr == pytest.approx(10.95, rel=1e-3)

    def test_price_spread_pct(self, fake_redis, aster_ws_snapshot):
        ws, ticker = aster_ws_snapshot
        collector = self._make_collector(fake_redis, ws, ticker)
        results = collector._build_normalized()
        btc = next(r for r in results if r.token == "BTC")
        # (65000 - 64900) / 64900 * 100 ≈ 0.154%
        assert btc.price_spread_pct == pytest.approx(0.1541, rel=1e-2)

    def test_oi_filter_excludes_low_oi(self, fake_redis, aster_ws_snapshot):
        ws, ticker = aster_ws_snapshot
        collector = self._make_collector(fake_redis, ws, ticker)
        collector._config = CollectorConfig(min_open_interest_usd=100_000_000.0)
        results = collector._build_normalized()
        assert results == []

    def test_volume_zero_excludes(self, fake_redis, aster_ws_snapshot):
        ws, ticker = aster_ws_snapshot
        # Set volume to zero
        ticker["BTCUSDT"]["quoteVolume"] = "0"
        ticker["ETHUSDT"]["quoteVolume"] = "0"
        collector = self._make_collector(fake_redis, ws, ticker)
        results = collector._build_normalized()
        assert results == []

    def test_empty_snapshots(self, fake_redis):
        collector = self._make_collector(fake_redis, {}, {})
        results = collector._build_normalized()
        assert results == []

    def test_ws_message_updates_snapshot(self, fake_redis, aster_ws_snapshot):
        ws, ticker = aster_ws_snapshot
        collector = self._make_collector(fake_redis, ws, ticker)
        event = {
            "e": "markPriceUpdate",
            "s": "BTCUSDT",
            "p": "66000",
            "i": "65900",
            "r": "0.0005",
            "T": 1700001000000,
        }
        collector._process_mark_price_event(event)
        assert collector._ws_snapshots["BTCUSDT"]["mark_price"] == pytest.approx(66000.0)
        assert collector._ws_snapshots["BTCUSDT"]["funding_rate"] == pytest.approx(0.0005)
