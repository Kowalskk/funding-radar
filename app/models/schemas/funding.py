"""
app/models/schemas/funding.py — Pydantic schemas for funding rate API responses.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class ExchangeInfo(BaseModel):
    """Compact exchange representation embedded in funding rate responses."""

    model_config = ConfigDict(from_attributes=True)

    slug: str
    name: str
    logo_url: str | None = None
    funding_interval_hours: int


class TokenInfo(BaseModel):
    """Compact token representation embedded in funding rate responses."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    name: str


# ── Single rate ────────────────────────────────────────────────────────────────


class FundingRateResponse(BaseModel):
    """Live or point-in-time funding rate for a token on one exchange."""

    model_config = ConfigDict(from_attributes=True)

    time: datetime
    exchange: ExchangeInfo
    token: TokenInfo

    # Core metrics
    funding_rate: Decimal = Field(..., description="Raw rate per funding interval")
    funding_rate_8h: Decimal | None = Field(None, description="Normalised to 8h basis")
    funding_apr: Decimal | None = Field(None, description="Annualised percentage rate")

    # Market context
    mark_price: Decimal | None = None
    index_price: Decimal | None = None
    open_interest_usd: Decimal | None = None
    volume_24h_usd: Decimal | None = None
    price_spread_pct: Decimal | None = None


class FundingRateSnapshot(BaseModel):
    """Current snapshot across all exchanges for a single token."""

    token: TokenInfo
    rates: list[FundingRateResponse]
    best_long: FundingRateResponse | None = Field(
        None, description="Exchange with the lowest (most negative) funding rate"
    )
    best_short: FundingRateResponse | None = Field(
        None, description="Exchange with the highest (most positive) funding rate"
    )


# ── History ────────────────────────────────────────────────────────────────────


class FundingHistoryPoint(BaseModel):
    """A single data point in the funding rate history chart."""

    time: datetime
    funding_rate: Decimal
    funding_apr: Decimal | None = None
    mark_price: Decimal | None = None


class FundingHistoryResponse(BaseModel):
    """Time-series history for a token on a specific exchange."""

    exchange: ExchangeInfo
    token: TokenInfo
    interval: str = Field(..., description="e.g. '1h', '4h', '1d'")
    data: list[FundingHistoryPoint]


# ── Market overview ────────────────────────────────────────────────────────────


class MarketOverviewRow(BaseModel):
    """A row in the market overview table (one token × one exchange)."""

    model_config = ConfigDict(from_attributes=True)

    token: TokenInfo
    exchange: ExchangeInfo
    funding_rate: Decimal
    funding_rate_8h: Decimal | None = None
    funding_apr: Decimal | None = None
    mark_price: Decimal | None = None
    open_interest_usd: Decimal | None = None
    volume_24h_usd: Decimal | None = None
    updated_at: datetime


class MarketOverviewResponse(BaseModel):
    """Full market overview: latest funding rates across all tokens and exchanges."""

    rows: list[MarketOverviewRow]
    generated_at: datetime
