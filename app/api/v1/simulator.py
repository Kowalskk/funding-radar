"""
app/api/v1/simulator.py — Funding carry trade P&L simulator endpoint.

Routes:
  POST /calculate — full simulation of a long/short carry trade
"""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator
from redis.asyncio import Redis

from app.api.deps import get_current_user_tier, get_redis_client, rate_limit
from app.processors.apr_calculator import calculate_pnl, calculate_breakeven_hours

router = APIRouter(prefix="/simulator", tags=["Simulator"])


# ── Request / Response models ─────────────────────────────────────────────────

class SimulateRequest(BaseModel):
    token: str = Field(..., description="Token symbol, e.g. 'BTC'")
    long_exchange: str = Field(..., description="Exchange slug for the long leg")
    short_exchange: str = Field(..., description="Exchange slug for the short leg")
    capital_usd: float = Field(..., gt=0, le=100_000_000, description="Notional USD per leg")
    days: float = Field(..., gt=0, le=365, description="Holding period in days")
    fee_type: Literal["maker", "taker"] = Field("taker", description="Fee tier to use")
    slippage_pct: float = Field(
        0.0, ge=0, le=5.0,
        description="Additional slippage assumption as % of notional (per leg entry and exit)"
    )

    @model_validator(mode="after")
    def legs_differ(self) -> "SimulateRequest":
        if self.long_exchange.lower() == self.short_exchange.lower():
            raise ValueError("long_exchange and short_exchange must be different.")
        return self


class LegResult(BaseModel):
    exchange: str
    side: str
    funding_apr: float
    funding_income_usd: float
    fee_usd: float
    slippage_usd: float


class SimulateResponse(BaseModel):
    token: str
    long_exchange: str
    short_exchange: str
    capital_usd: float
    days: float
    fee_type: str

    long_leg: LegResult
    short_leg: LegResult

    # Aggregate
    gross_funding_delta_apr: float
    gross_funding_pnl_usd: float
    total_fees_usd: float
    total_slippage_usd: float
    net_pnl_usd: float
    net_apr: float

    # Break-even
    breakeven_days: float | None
    breakeven_hours: float | None

    # Market context
    long_mark_price: float | None
    short_mark_price: float | None
    long_funding_rate_8h: float | None
    short_funding_rate_8h: float | None

    # Data quality
    long_data_stale: bool
    short_data_stale: bool


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post(
    "/calculate",
    summary="Simulate a delta-neutral funding carry trade",
    response_model=SimulateResponse,
)
async def calculate_simulation(
    body: SimulateRequest,
    _rl: None = Depends(rate_limit),
    tier: str = Depends(get_current_user_tier),
    redis: Redis = Depends(get_redis_client),
) -> Any:
    """
    Simulates the P&L of entering a delta-neutral position:
    - **Long** on `long_exchange` (pays / collects `long_apr` funding)
    - **Short** on `short_exchange` (pays / collects `short_apr` funding)

    Returns a detailed breakdown: per-leg income, fees, slippage, net P&L, and APR.

    Uses live funding data from Redis (`funding:latest:{exchange}:{token}`).
    """
    token_upper = body.token.upper()
    long_ex = body.long_exchange.lower()
    short_ex = body.short_exchange.lower()

    # Fetch live snapshots from Redis per-asset cache
    long_raw = await redis.get(f"funding:latest:{long_ex}:{token_upper}")
    short_raw = await redis.get(f"funding:latest:{short_ex}:{token_upper}")

    # Fallback: try canonical-ranked data
    if not long_raw or not short_raw:
        raw_ranked = await redis.get("funding:ranked")
        if raw_ranked:
            ranked = json.loads(raw_ranked)
            for item in ranked:
                if item.get("token") == token_upper:
                    for row in item.get("rows", []):
                        if row["exchange"] == long_ex and not long_raw:
                            long_raw = json.dumps(row)
                        if row["exchange"] == short_ex and not short_raw:
                            short_raw = json.dumps(row)
                    break

    if not long_raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No live data for {token_upper} on {body.long_exchange}.",
        )
    if not short_raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No live data for {token_upper} on {body.short_exchange}.",
        )

    long_data = json.loads(long_raw)
    short_data = json.loads(short_raw)

    # Pick fee rate based on fee_type
    fee_key = "taker_fee" if body.fee_type == "taker" else "maker_fee"
    long_fee_pct  = float(long_data.get(fee_key, long_data.get("taker_fee", 0.035)))
    short_fee_pct = float(short_data.get(fee_key, short_data.get("taker_fee", 0.035)))
    long_apr  = float(long_data.get("funding_apr", 0))
    short_apr = float(short_data.get("funding_apr", 0))

    # For the long leg we pay funding (negative means we collect)
    # The key insight: your net is short_apr - long_apr (sign-aware)
    # For simplicity, treat the delta as the gross yield
    funding_delta_apr = short_apr - long_apr

    # Entry + exit fees per leg (one-way entry: long + short; one-way exit: same)
    entry_fee_pct = (long_fee_pct + short_fee_pct) + body.slippage_pct * 2
    exit_fee_pct  = (long_fee_pct + short_fee_pct) + body.slippage_pct * 2

    pnl = calculate_pnl(
        funding_apr=funding_delta_apr,
        capital_usd=body.capital_usd,
        days=body.days,
        entry_fee_pct=entry_fee_pct,
        exit_fee_pct=exit_fee_pct,
    )

    total_slippage_usd = body.slippage_pct / 100 * body.capital_usd * 4  # 2 legs × 2 sides
    fee_excl_slippage = (long_fee_pct + short_fee_pct) * 2 / 100 * body.capital_usd

    # Per-leg funding income (absolute, based on individual APR × capital × days)
    long_income  = (long_apr / 100) * body.capital_usd * (body.days / 365)
    short_income = (short_apr / 100) * body.capital_usd * (body.days / 365)

    breakeven_total_fee_pct = (long_fee_pct + short_fee_pct) * 2 + body.slippage_pct * 4
    breakeven_hours = calculate_breakeven_hours(
        abs(funding_delta_apr), breakeven_total_fee_pct
    )
    breakeven_days = (breakeven_hours / 24) if breakeven_hours else None

    return SimulateResponse(
        token=token_upper,
        long_exchange=long_ex,
        short_exchange=short_ex,
        capital_usd=body.capital_usd,
        days=body.days,
        fee_type=body.fee_type,
        long_leg=LegResult(
            exchange=long_ex,
            side="long",
            funding_apr=round(long_apr, 4),
            funding_income_usd=round(long_income, 4),
            fee_usd=round(long_fee_pct / 100 * body.capital_usd * 2, 4),
            slippage_usd=round(body.slippage_pct / 100 * body.capital_usd * 2, 4),
        ),
        short_leg=LegResult(
            exchange=short_ex,
            side="short",
            funding_apr=round(short_apr, 4),
            funding_income_usd=round(short_income, 4),
            fee_usd=round(short_fee_pct / 100 * body.capital_usd * 2, 4),
            slippage_usd=round(body.slippage_pct / 100 * body.capital_usd * 2, 4),
        ),
        gross_funding_delta_apr=round(funding_delta_apr, 4),
        gross_funding_pnl_usd=round(pnl["gross_pnl_usd"], 4),
        total_fees_usd=round(fee_excl_slippage, 4),
        total_slippage_usd=round(total_slippage_usd, 4),
        net_pnl_usd=round(pnl["net_pnl_usd"], 4),
        net_apr=round(pnl["net_apr"], 4),
        breakeven_days=round(breakeven_days, 2) if breakeven_days else None,
        breakeven_hours=round(breakeven_hours, 1) if breakeven_hours else None,
        long_mark_price=float(long_data.get("mark_price") or 0) or None,
        short_mark_price=float(short_data.get("mark_price") or 0) or None,
        long_funding_rate_8h=float(long_data.get("funding_rate_8h") or 0) or None,
        short_funding_rate_8h=float(short_data.get("funding_rate_8h") or 0) or None,
        long_data_stale=bool(long_data.get("is_stale", False)),
        short_data_stale=bool(short_data.get("is_stale", False)),
    )
