"""
app/api/v1/arbitrage.py — Cross-exchange arbitrage opportunity endpoints.

Routes:
  GET /opportunities — ranked list read from Redis "arbitrage:current"
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from redis.asyncio import Redis

from app.api.deps import get_current_user_tier, get_redis_client, rate_limit

router = APIRouter(prefix="/arbitrage", tags=["Arbitrage"])


@router.get(
    "/opportunities",
    summary="Current delta-neutral arbitrage opportunities",
    response_model=dict,
)
async def get_opportunities(
    # Filters
    min_apr: float = Query(0.0, description="Minimum net APR after taker fees (%)"),
    min_oi: float = Query(0.0, description="Minimum open interest per leg (USD)"),
    exchanges: list[str] = Query(default=[], description="Only include these exchange slugs"),
    token: str | None = Query(None, description="Filter by token symbol (e.g. BTC)"),
    # Pagination
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    # Auth
    _rl: None = Depends(rate_limit),
    tier: str = Depends(get_current_user_tier),
    redis: Redis = Depends(get_redis_client),
) -> Any:
    """
    Returns delta-neutral funding rate arbitrage opportunities.

    Each opportunity contains:
    - **long_leg**: exchange where funding APR is lowest (you collect / pay least)
    - **short_leg**: exchange where funding APR is highest (counterparty pays you)
    - **net_apr_taker**: annualised yield after taker-fee round-trip cost
    - **breakeven_hours_taker**: hours needed for carry to cover fees

    Results are sorted by `net_apr_taker` descending.
    Reads from the in-memory Redis key `arbitrage:current` (refreshed every ~5 s).
    """
    raw = await redis.get("arbitrage:current")
    if not raw:
        return {
            "data": [],
            "total": 0,
            "message": "No arbitrage data available yet — collectors may still be warming up.",
        }

    opportunities: list[dict] = json.loads(raw)

    # ── Filters ───────────────────────────────────────────────────────────────
    if min_apr > 0:
        opportunities = [o for o in opportunities if o.get("net_apr_taker", 0) >= min_apr]

    if token:
        token_upper = token.upper()
        opportunities = [o for o in opportunities if o.get("token") == token_upper]

    if exchanges:
        ex_set = {e.lower() for e in exchanges}
        opportunities = [
            o for o in opportunities
            if o.get("long_leg", {}).get("exchange") in ex_set
            or o.get("short_leg", {}).get("exchange") in ex_set
        ]

    if min_oi > 0:
        opportunities = [
            o for o in opportunities
            if (o.get("long_leg", {}).get("open_interest_usd") or 0) >= min_oi
            and (o.get("short_leg", {}).get("open_interest_usd") or 0) >= min_oi
        ]

    # ── Tier enforcement — free users get top 10 only ─────────────────────────
    if tier in ("anonymous", "free") and offset + limit > 10:
        limit = max(0, 10 - offset)
        if limit == 0:
            return {
                "data": [],
                "total": len(opportunities),
                "limit": limit,
                "offset": offset,
                "tier_note": "Free tier is limited to the top 10 opportunities. Upgrade to Pro for full access.",
            }

    total = len(opportunities)
    page = opportunities[offset : offset + limit]

    response: dict = {
        "data": page,
        "total": total,
        "limit": limit,
        "offset": offset,
    }
    if tier in ("anonymous", "free") and total > 10:
        response["tier_note"] = (
            "Free tier is limited to the top 10 opportunities. Upgrade to Pro for full access."
        )
    return response


@router.get(
    "/opportunities/{token}",
    summary="Best arbitrage opportunity for a specific token",
    response_model=dict,
)
async def get_opportunity_for_token(
    token: str,
    _rl: None = Depends(rate_limit),
    tier: str = Depends(get_current_user_tier),
    redis: Redis = Depends(get_redis_client),
) -> Any:
    """Return the single best arbitrage opportunity for **{token}** (highest net APR)."""
    raw = await redis.get("arbitrage:current")
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Arbitrage data not yet available.",
        )

    token_upper = token.upper()
    opportunities: list[dict] = json.loads(raw)
    match = next(
        (o for o in opportunities if o.get("token") == token_upper),
        None,
    )
    if match is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No arbitrage opportunity found for token '{token_upper}'.",
        )
    return match
