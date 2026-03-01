"""app/processors — Data processing: normalisation, arbitrage, ranking."""

from app.processors.apr_calculator import (
    annualise,
    calculate_breakeven_hours,
    calculate_pnl,
    funding_to_8h,
    funding_to_apr,
)
from app.processors.normalizer import DataNormalizer, ExchangeSnapshot, TokenView
from app.processors.arbitrage_calculator import ArbitrageCalculator, ArbitrageResult, ArbitrageLeg
from app.processors.funding_aggregator import FundingAggregator, TokenRankRow, ExchangeRateRow

__all__ = [
    # Utilities
    "funding_to_8h", "funding_to_apr", "annualise",
    "calculate_pnl", "calculate_breakeven_hours",
    # Normalizer
    "DataNormalizer", "ExchangeSnapshot", "TokenView",
    # Calculators
    "ArbitrageCalculator", "ArbitrageResult", "ArbitrageLeg",
    "FundingAggregator", "TokenRankRow", "ExchangeRateRow",
]
