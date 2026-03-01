"""
tests/test_processors/test_arbitrage.py — Unit tests for ArbitrageCalculator.
"""

from __future__ import annotations

import pytest

from app.processors.arbitrage_calculator import ArbitrageCalculator
from app.collectors.base import NormalizedFundingData


def _make_data(
    exchange: str,
    token: str,
    funding_rate: float,
    funding_interval_hours: int = 8,
    mark_price: float = 65000.0,
    index_price: float | None = None,
    open_interest_usd: float = 50_000_000.0,
    volume_24h_usd: float = 100_000_000.0,
    maker_fee: float = 0.01,
    taker_fee: float = 0.035,
) -> NormalizedFundingData:
    periods_per_year = (8760 / funding_interval_hours)
    funding_rate_8h = funding_rate * (8 / funding_interval_hours)
    funding_apr = funding_rate * periods_per_year * 100
    index = index_price if index_price is not None else mark_price
    return NormalizedFundingData(
        exchange=exchange,
        token=token,
        symbol=f"{token}USDT",
        funding_rate=funding_rate,
        funding_rate_8h=funding_rate_8h,
        funding_apr=funding_apr,
        funding_interval_hours=funding_interval_hours,
        next_funding_time=None,
        predicted_rate=None,
        mark_price=mark_price,
        index_price=index,
        open_interest_usd=open_interest_usd,
        volume_24h_usd=volume_24h_usd,
        price_spread_pct=abs(mark_price - index) / max(index, 1e-9) * 100,
        maker_fee=maker_fee,
        taker_fee=taker_fee,
        timestamp=1_700_000_000_000,
        is_live=True,
    )


class TestArbitrageCalculator:
    def _make_calc(self) -> ArbitrageCalculator:
        return ArbitrageCalculator(min_funding_delta_apr=1.0)

    def test_finds_basic_opportunity(self):
        calc = self._make_calc()
        data = [
            _make_data("hyperliquid", "BTC", funding_rate=0.0001, funding_interval_hours=1),
            _make_data("aster",       "BTC", funding_rate=0.0004, funding_interval_hours=8),
        ]
        opps = calc.calculate(data)
        assert len(opps) == 1
        opp = opps[0]
        assert opp.token == "BTC"
        # Long HL (lower funding), Short Aster (higher funding)
        assert opp.long_leg.exchange == "hyperliquid"
        assert opp.short_leg.exchange == "aster"
        assert opp.funding_delta_apr > 0

    def test_net_apr_less_than_gross(self):
        """Net APR should be lower than gross delta due to fees."""
        calc = self._make_calc()
        data = [
            _make_data("hyperliquid", "BTC", funding_rate=0.0001, funding_interval_hours=1),
            _make_data("aster",       "BTC", funding_rate=0.0004, funding_interval_hours=8),
        ]
        opp = calc.calculate(data)[0]
        assert opp.net_apr_taker < opp.funding_delta_apr
        assert opp.net_apr_maker < opp.net_apr_taker  # maker cheaper

    def test_single_exchange_no_opportunity(self):
        """Single exchange → no cross-exchange arb possible."""
        calc = self._make_calc()
        data = [
            _make_data("hyperliquid", "BTC", funding_rate=0.0001),
        ]
        assert calc.calculate(data) == []

    def test_low_delta_filtered(self):
        """delta < min_funding_delta_apr → excluded."""
        calc = ArbitrageCalculator(min_funding_delta_apr=100.0)  # very high threshold
        data = [
            _make_data("hyperliquid", "BTC", funding_rate=0.0001),
            _make_data("aster",       "BTC", funding_rate=0.0002),
        ]
        assert calc.calculate(data) == []

    def test_multiple_tokens(self):
        """Opportunities computed independently per token."""
        calc = self._make_calc()
        data = [
            _make_data("hyperliquid", "BTC", funding_rate=0.0001),
            _make_data("aster",       "BTC", funding_rate=0.0004),
            _make_data("hyperliquid", "ETH", funding_rate=0.00002),
            _make_data("aster",       "ETH", funding_rate=0.0003),
        ]
        opps = calc.calculate(data)
        tokens = {o.token for o in opps}
        assert "BTC" in tokens
        assert "ETH" in tokens

    def test_results_sorted_by_net_apr_desc(self):
        calc = self._make_calc()
        data = [
            _make_data("hyperliquid", "BTC", funding_rate=0.0001),
            _make_data("aster",       "BTC", funding_rate=0.0004),
            _make_data("hyperliquid", "ETH", funding_rate=0.00002),
            _make_data("aster",       "ETH", funding_rate=0.0003),
        ]
        opps = calc.calculate(data)
        net_aprs = [o.net_apr_taker for o in opps]
        assert net_aprs == sorted(net_aprs, reverse=True)

    def test_price_spread_computed(self):
        calc = self._make_calc()
        data = [
            _make_data("hyperliquid", "BTC", funding_rate=0.0001,
                       mark_price=65000.0, index_price=64900.0),
            _make_data("aster",       "BTC", funding_rate=0.0004,
                       mark_price=65100.0, index_price=65000.0),
        ]
        opp = calc.calculate(data)[0]
        # price_spread_pct should reflect mark price difference
        assert opp.price_spread_pct >= 0.0

    def test_min_open_interest_considered(self):
        """Opportunity should reference the minimum OI of the two legs."""
        calc = self._make_calc()
        data = [
            _make_data("hyperliquid", "BTC", funding_rate=0.0001, open_interest_usd=20_000_000),
            _make_data("aster",       "BTC", funding_rate=0.0004, open_interest_usd=50_000_000),
        ]
        opp = calc.calculate(data)[0]
        assert opp.min_open_interest_usd == pytest.approx(20_000_000.0)
