"""
app/api/v1/exchanges.py — Exchange catalogue endpoints.

Routes:
  GET /exchanges                    — list all active exchanges with live stats
  GET /exchanges/{slug}/tokens      — tokens available on a specific exchange
"""


import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from redis.asyncio import Redis
from sqlalchemy import select

from app.api.deps import get_current_user_tier, get_redis_client, rate_limit
from app.core.database import get_db_session
from app.models.db.exchange import Exchange
from app.models.db.token import Token

router = APIRouter(prefix="/exchanges", tags=["Exchanges"])


# ── GET /exchanges ─────────────────────────────────────────────────────────────

@router.get(
    "",
    summary="List all active exchanges with live funding stats",
    response_model=dict,
)
async def list_exchanges(
    _rl: None = Depends(rate_limit),
    tier: str = Depends(get_current_user_tier),
    redis: Redis = Depends(get_redis_client),
) -> Any:
    """
    Returns all active exchanges with:
    - Exchange metadata (fees, funding interval)
    - Live token count from Redis ranked data
    - Number of live arbitrage opportunities involving this exchange
    """
    # Load exchanges from DB
    async with get_db_session() as session:
        result = await session.execute(
            select(Exchange).where(Exchange.is_active.is_(True))
        )
        exchanges = result.scalars().all()

    # Enrich with live stats from Redis
    raw_ranked = await redis.get("funding:ranked")
    raw_arb = await redis.get("arbitrage:current")

    ranked_list: list[dict] = json.loads(raw_ranked) if raw_ranked else []
    arb_list: list[dict] = json.loads(raw_arb) if raw_arb else []

    # Build per-exchange token counts from ranked data
    token_count_by_exchange: dict[str, int] = {}
    for item in ranked_list:
        for row in item.get("rows", []):
            ex = row.get("exchange", "")
            token_count_by_exchange[ex] = token_count_by_exchange.get(ex, 0) + 1

    # Build arb opportunity count per exchange
    arb_count_by_exchange: dict[str, int] = {}
    for opp in arb_list:
        for leg_key in ("long_leg", "short_leg"):
            ex = opp.get(leg_key, {}).get("exchange", "")
            if ex:
                arb_count_by_exchange[ex] = arb_count_by_exchange.get(ex, 0) + 1

    data = [
        {
            "slug": ex.slug,
            "name": ex.name,
            "logo_url": ex.logo_url,
            "maker_fee": float(ex.maker_fee),
            "taker_fee": float(ex.taker_fee),
            "funding_interval_hours": ex.funding_interval_hours,
            "is_active": ex.is_active,
            # Live stats
            "live_token_count": token_count_by_exchange.get(ex.slug, 0),
            "arb_opportunity_count": arb_count_by_exchange.get(ex.slug, 0),
        }
        for ex in exchanges
    ]

    # Sort: most tokens first
    data.sort(key=lambda e: e["live_token_count"], reverse=True)

    return {"data": data, "total": len(data)}


# ── GET /exchanges/{slug}/tokens ──────────────────────────────────────────────

@router.get(
    "/{slug}/tokens",
    summary="Tokens listed on a specific exchange with live funding rates",
    response_model=dict,
)
async def get_exchange_tokens(
    slug: str,
    # Filters
    min_oi: float = Query(0.0, description="Minimum open interest USD"),
    sort_by: str = Query("funding_apr", description="Sort field: funding_apr | oi | volume"),
    # Pagination
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    # Auth
    _rl: None = Depends(rate_limit),
    tier: str = Depends(get_current_user_tier),
    redis: Redis = Depends(get_redis_client),
) -> Any:
    """
    Returns all tokens available on **{slug}** with their current funding rates.

    Data is sourced from the live Redis snapshot for freshness.
    Falls back to the DB token catalogue if Redis has no data.
    """
    slug_lower = slug.lower()

    # Verify exchange exists
    async with get_db_session() as session:
        exchange = await session.scalar(
            select(Exchange).where(
                Exchange.slug == slug_lower, Exchange.is_active.is_(True)
            )
        )
        if exchange is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Exchange '{slug}' not found or inactive.",
            )

    # Pull live rows for this exchange from Redis ranked data
    raw_ranked = await redis.get("funding:ranked")
    live_rows: list[dict] = []

    if raw_ranked:
        ranked = json.loads(raw_ranked)
        for item in ranked:
            for row in item.get("rows", []):
                if row.get("exchange") == slug_lower:
                    live_rows.append({**row, "token": item["token"]})

    # Apply filters
    if min_oi > 0:
        live_rows = [r for r in live_rows if r.get("open_interest_usd", 0) >= min_oi]

    # Sort
    sort_key_map = {
        "funding_apr": lambda r: r.get("funding_apr", 0),
        "oi": lambda r: r.get("open_interest_usd", 0),
        "volume": lambda r: r.get("volume_24h_usd", 0),
    }
    sort_fn = sort_key_map.get(sort_by, sort_key_map["funding_apr"])
    live_rows.sort(key=sort_fn, reverse=True)

    total = len(live_rows)
    page = live_rows[offset : offset + limit]

    return {
        "exchange": {
            "slug": exchange.slug,
            "name": exchange.name,
            "maker_fee": float(exchange.maker_fee),
            "taker_fee": float(exchange.taker_fee),
            "funding_interval_hours": exchange.funding_interval_hours,
        },
        "data": page,
        "total": total,
        "limit": limit,
        "offset": offset,
        "source": "live" if raw_ranked else "db",
    }
