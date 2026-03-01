"""
app/models/schemas/arbitrage.py — Pydantic schemas for cross-exchange arbitrage opportunities.

An arbitrage opportunity exists when the funding rate spread between two exchanges
for the same token is large enough to cover fees and generate a net profit.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class ArbitrageLeg(BaseModel):
    """One side of an arbitrage pair (long or short position)."""

    exchange_slug: str
    exchange_name: str
    logo_url: str | None = None

    # Position direction
    side: str = Field(..., description="'long' or 'short'")

    # Funding data
    funding_rate: Decimal
    funding_rate_8h: Decimal | None = None
    funding_apr: Decimal | None = None

    # Costs
    taker_fee_pct: Decimal = Field(..., description="Taker fee in percent, e.g. 0.035")

    # Market liquidity context
    mark_price: Decimal | None = None
    open_interest_usd: Decimal | None = None


class ArbitrageOpportunity(BaseModel):
    """A delta-neutral arbitrage: long on one exchange, short on another."""

    token_symbol: str
    token_name: str

    long_leg: ArbitrageLeg   # pays the lowest (most negative) funding rate
    short_leg: ArbitrageLeg  # receives the highest (most positive) funding rate

    # Spread metrics (period = 8h)
    spread_8h: Decimal = Field(
        ..., description="Gross funding spread over 8h (short APR - long APR) / (3*365)"
    )
    spread_apr: Decimal = Field(..., description="Annualised gross spread in percent")

    # Net profitability (after round-trip taker fees on both legs)
    net_apr_after_fees: Decimal = Field(
        ..., description="spread_apr minus total round-trip trading fees (annualised)"
    )
    entry_fee_cost_pct: Decimal = Field(
        ..., description="Total one-way entry cost as % (sum of both taker fees)"
    )

    # Break-even holding time
    breakeven_hours: Decimal | None = Field(
        None, description="Hours of carry needed to cover entry fees"
    )

    detected_at: datetime


class ArbitrageListResponse(BaseModel):
    """Paginated list of current arbitrage opportunities."""

    opportunities: list[ArbitrageOpportunity]
    total: int
    generated_at: datetime

    # Applied filters (echoed back)
    min_net_apr: Decimal | None = None
    token_filter: str | None = None
    exchange_filter: list[str] | None = None
