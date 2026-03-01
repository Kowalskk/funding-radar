"""
app/processors/apr_calculator.py — Pure utility functions for funding rate maths.

All functions are stateless and operate on plain floats so they can be used
from any async or sync context without side effects.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


# ── Unit conversions ──────────────────────────────────────────────────────────


def funding_to_8h(rate: float, interval_hours: int) -> float:
    """Normalise a per-funding-interval rate to an equivalent 8-hour rate.

    Examples:
      • Hyperliquid (1h):  rate_1h × 8   = rate_8h
      • Aster       (8h):  rate_8h × 1   = rate_8h  (no-op)
      • dYdX        (8h):  rate_8h × 1   = rate_8h
    """
    if interval_hours <= 0:
        raise ValueError(f"interval_hours must be positive, got {interval_hours}")
    return rate * (8 / interval_hours)


def funding_to_apr(rate_8h: float) -> float:
    """Convert an 8-hour funding rate to an annualised percentage rate (APR).

    There are 3 funding periods per day (24h / 8h = 3) × 365 days = 1 095
    periods per year.

    APR (%) = rate_8h × 1 095 × 100
    """
    return rate_8h * 3 * 365 * 100


def apr_to_8h_rate(apr: float) -> float:
    """Inverse of `funding_to_apr` — APR (%) back to per-8h rate."""
    return apr / (3 * 365 * 100)


def funding_to_daily(rate_8h: float) -> float:
    """Convert an 8-hour rate to a daily rate (3 periods per day)."""
    return rate_8h * 3


def annualise(rate: float, interval_hours: int) -> float:
    """One-shot helper: raw funding rate → APR regardless of interval."""
    return funding_to_apr(funding_to_8h(rate, interval_hours))


# ── Fee calculations ──────────────────────────────────────────────────────────


def round_trip_fee_apr(taker_fee_pct: float) -> float:
    """Annualise the round-trip (entry + exit) taker fee cost.

    Expressed as an APR so it is directly comparable to funding APR.
    One full round-trip = 2× taker_fee (open + close on one leg).
    For a delta-neutral strategy that is 4× taker_fee total (both legs).

    Returns APR in % (positive = cost).
    """
    # 4 transactions: open-long, close-long, open-short, close-short
    # Annualised by dividing by 1 year in 8h periods and multiplying to get %
    # Break-even APR: fees / 1 = fees (they are paid once at entry+exit)
    # This is a one-time cost; annualise by (365 / holding_days)
    # Returned raw here — caller annualises based on holding period.
    return taker_fee_pct * 4  # total % cost of round-trip (both legs, entry+exit)


def entry_exit_fee_pct(taker_fee_pct_a: float, taker_fee_pct_b: float) -> float:
    """Total one-way entry cost as % of notional (open both legs)."""
    return taker_fee_pct_a + taker_fee_pct_b


# ── P&L simulation ────────────────────────────────────────────────────────────


def calculate_pnl(
    funding_apr: float,
    capital_usd: float,
    days: float,
    entry_fee_pct: float = 0.0,
    exit_fee_pct: float = 0.0,
) -> dict[str, float]:
    """Simulate the net P&L of a carry trade position.

    Args:
        funding_apr:    Gross annualised funding yield in % (net of both legs).
        capital_usd:    Notional size in USD per leg.
        days:           Holding period in days.
        entry_fee_pct:  One-way entry fee as % of capital (both legs combined).
        exit_fee_pct:   One-way exit fee as % of capital (both legs combined).

    Returns a dict with:
        gross_pnl_usd:  Funding earned before fees.
        entry_fee_usd:  Fee paid on entry.
        exit_fee_usd:   Fee paid on exit.
        net_pnl_usd:    Gross − all fees.
        net_apr:        Realised APR based on holding period.
        breakeven_days: Days of carry needed to cover total fees.
    """
    gross_pnl_usd = (funding_apr / 100) * capital_usd * (days / 365)
    entry_fee_usd = (entry_fee_pct / 100) * capital_usd
    exit_fee_usd = (exit_fee_pct / 100) * capital_usd
    net_pnl_usd = gross_pnl_usd - entry_fee_usd - exit_fee_usd

    total_fees_usd = entry_fee_usd + exit_fee_usd
    daily_yield_usd = (funding_apr / 100) * capital_usd / 365
    breakeven_days = (total_fees_usd / daily_yield_usd) if daily_yield_usd > 0 else None

    net_apr = (net_pnl_usd / capital_usd) * (365 / days) * 100 if days > 0 else 0.0

    return {
        "gross_pnl_usd": round(gross_pnl_usd, 4),
        "entry_fee_usd": round(entry_fee_usd, 4),
        "exit_fee_usd": round(exit_fee_usd, 4),
        "net_pnl_usd": round(net_pnl_usd, 4),
        "net_apr": round(net_apr, 4),
        "breakeven_days": round(breakeven_days, 2) if breakeven_days else None,
    }


def calculate_breakeven_hours(
    funding_apr: float,
    total_fee_pct: float,  # combined round-trip fee %
) -> float | None:
    """Hours of carry required to pay for total round-trip fees.

    Args:
        funding_apr:    Gross annualised yield in % (positive = income).
        total_fee_pct:  Total fee cost as % of notional (entry + exit, both legs).

    Returns hours as float, or None if funding_apr <= 0.
    """
    if funding_apr <= 0:
        return None
    # hourly_yield = apr / (365 * 24)
    hourly_yield_pct = funding_apr / (365 * 24)
    return total_fee_pct / hourly_yield_pct
