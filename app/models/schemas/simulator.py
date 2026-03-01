"""
app/models/schemas/simulator.py — Pydantic schemas for the funding rate carry simulator.

The simulator calculates the P&L of entering a delta-neutral position
(long on exchange A, short on exchange B) and holding it for N hours.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field, model_validator


class SimulatorLegConfig(BaseModel):
    """Configuration for one leg of the simulated position."""

    exchange_slug: str = Field(..., description="Exchange to open the position on")
    side: str = Field(..., description="'long' or 'short'")
    notional_usd: Decimal = Field(..., gt=0, description="Position size in USD")
    leverage: Decimal = Field(default=Decimal("1"), ge=1, le=100)
    taker_fee_pct: Decimal = Field(
        ..., ge=0, description="Override taker fee in %; leave 0 to use exchange default"
    )

    @model_validator(mode="after")
    def validate_side(self) -> SimulatorLegConfig:
        if self.side not in {"long", "short"}:
            raise ValueError("side must be 'long' or 'short'")
        return self


class SimulatorRequest(BaseModel):
    """Request body for the P&L simulation endpoint."""

    token_symbol: str = Field(..., description="Token to simulate (e.g. 'BTC')")
    holding_hours: Decimal = Field(
        ..., gt=0, le=8760, description="Holding period in hours (max 1 year)"
    )
    long_leg: SimulatorLegConfig
    short_leg: SimulatorLegConfig

    @model_validator(mode="after")
    def legs_must_differ(self) -> SimulatorRequest:
        if self.long_leg.exchange_slug == self.short_leg.exchange_slug:
            raise ValueError("long and short legs must be on different exchanges")
        if self.long_leg.side == self.short_leg.side:
            raise ValueError("legs must have opposite sides")
        return self


class SimulatorLegResult(BaseModel):
    """Simulation result for a single leg."""

    exchange_slug: str
    exchange_name: str
    side: str

    # Funding received/paid
    funding_periods: Decimal = Field(..., description="Number of funding intervals elapsed")
    funding_pnl_usd: Decimal = Field(
        ..., description="Cumulative funding P&L (positive = received, negative = paid)"
    )
    funding_rate_used: Decimal
    funding_apr_used: Decimal | None = None

    # Cost
    entry_fee_usd: Decimal
    exit_fee_usd: Decimal
    total_fee_usd: Decimal


class SimulatorResponse(BaseModel):
    """Full P&L breakdown for a simulated carry trade."""

    token_symbol: str
    holding_hours: Decimal

    long_leg: SimulatorLegResult
    short_leg: SimulatorLegResult

    # Aggregate metrics
    gross_funding_pnl_usd: Decimal = Field(
        ..., description="Sum of funding P&L across both legs"
    )
    total_fees_usd: Decimal = Field(..., description="Sum of all entry + exit fees")
    net_pnl_usd: Decimal = Field(
        ..., description="gross_funding_pnl_usd - total_fees_usd"
    )
    net_apr: Decimal = Field(..., description="Annualised net P&L as % of total notional")

    # Break-even
    breakeven_hours: Decimal | None = Field(
        None, description="Hours needed for funding to cover all fees"
    )

    # Input echo
    total_notional_usd: Decimal
    assumed_funding_interval_hours_long: int
    assumed_funding_interval_hours_short: int
