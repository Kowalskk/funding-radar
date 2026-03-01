"""
app/api/v1/funding.py — Funding rate REST endpoints.

Routes:
  GET /rates                 — live cross-exchange ranked list from Redis
  GET /history/{token}       — historical series from TimescaleDB
  GET /token/{token}         — full token detail (snapshot + history summary + arb)
"""


import json
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from redis.asyncio import Redis
from sqlalchemy import desc, func, select, text

from app.api.deps import get_current_user_tier, get_redis_client, rate_limit
from app.core.database import get_db_session
from app.models.db.exchange import Exchange
from app.models.db.funding_rate import FundingRate
from app.models.db.token import Token

router = APIRouter(prefix="/funding", tags=["Funding Rates"])

# ── Timeframe helpers ─────────────────────────────────────────────────────────

_TIMEFRAME_DELTA: dict[str, timedelta] = {
    "live": timedelta(minutes=5),
    "1h":  timedelta(hours=1),
    "8h":  timedelta(hours=8),
    "24h": timedelta(hours=24),
    "3d":  timedelta(days=3),
    "7d":  timedelta(days=7),
    "15d": timedelta(days=15),
    "31d": timedelta(days=31),
}


def _timeframe_to_delta(tf: str) -> timedelta:
    delta = _TIMEFRAME_DELTA.get(tf)
    if delta is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid timeframe '{tf}'. Valid: {list(_TIMEFRAME_DELTA)}",
        )
    return delta


# ── GET /funding/rates ─────────────────────────────────────────────────────────

@router.get(
    "/rates",
    summary="Live cross-exchange funding rates ranked by APR",
    response_model=dict,
)
async def get_funding_rates(
    # Filters
    timeframe: str = Query("live", description="live | 1h | 8h | 24h | 3d | 7d | 15d | 31d"),
    exchanges: list[str] = Query(default=[], description="Filter by exchange slugs"),
    token: str | None = Query(None, description="Filter by token symbol (e.g. BTC)"),
    # Pagination
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    # Auth / rate limit
    _rl: None = Depends(rate_limit),
    tier: str = Depends(get_current_user_tier),
    redis: Redis = Depends(get_redis_client),
) -> Any:
    """
    Returns tokens ranked by their maximum funding APR across all exchanges.

    - **timeframe=live**: reads the in-memory Redis snapshot (fastest, real-time).
    - **timeframe=1h+**: aggregates data from TimescaleDB.
    """
    _timeframe_to_delta(timeframe)  # validate early

    if timeframe == "live":
        return await _live_rates(redis, exchanges, token, limit, offset)
    else:
        return await _historical_rates(timeframe, exchanges, token, limit, offset)


async def _live_rates(
    redis: Redis,
    exchanges: list[str],
    token: str | None,
    limit: int,
    offset: int,
) -> dict:
    raw = await redis.get("funding:ranked")
    if not raw:
        return {"data": [], "total": 0, "source": "live", "message": "No live data yet."}

    ranked: list[dict] = json.loads(raw)

    # Apply filters
    if token:
        ranked = [r for r in ranked if r["token"].upper() == token.upper()]
    if exchanges:
        ex_set = {e.lower() for e in exchanges}
        ranked = [
            {**r, "rows": [row for row in r["rows"] if row["exchange"] in ex_set]}
            for r in ranked
        ]
        ranked = [r for r in ranked if r["rows"]]

    total = len(ranked)
    page = ranked[offset : offset + limit]
    return {
        "data": page,
        "total": total,
        "limit": limit,
        "offset": offset,
        "source": "live",
    }


async def _historical_rates(
    timeframe: str,
    exchanges: list[str],
    token: str | None,
    limit: int,
    offset: int,
) -> dict:
    """Aggregate average funding APR from TimescaleDB over the timeframe window."""
    delta = _timeframe_to_delta(timeframe)
    since = datetime.now(tz=timezone.utc) - delta

    async with get_db_session() as session:
        q = (
            select(
                Token.symbol,
                Exchange.slug,
                Exchange.name,
                func.avg(FundingRate.funding_apr).label("avg_apr"),
                func.max(FundingRate.funding_apr).label("max_apr"),
                func.min(FundingRate.funding_apr).label("min_apr"),
                func.avg(FundingRate.open_interest_usd).label("avg_oi"),
                func.avg(FundingRate.volume_24h_usd).label("avg_vol"),
                func.max(FundingRate.time).label("latest_time"),
            )
            .join(Exchange, FundingRate.exchange_id == Exchange.id)
            .join(Token, FundingRate.token_id == Token.id)
            .where(FundingRate.time >= since)
        )

        if token:
            q = q.where(Token.symbol == token.upper())
        if exchanges:
            q = q.where(Exchange.slug.in_([e.lower() for e in exchanges]))

        q = q.group_by(Token.symbol, Exchange.slug, Exchange.name).order_by(
            desc("avg_apr")
        )

        result = await session.execute(q)
        rows = result.all()

    # Group by token
    token_map: dict[str, dict] = {}
    for row in rows:
        sym = row.symbol
        if sym not in token_map:
            token_map[sym] = {
                "token": sym,
                "max_apr": 0.0,
                "min_apr": float("inf"),
                "spread_apr": 0.0,
                "exchange_count": 0,
                "rows": [],
            }
        apr = float(row.avg_apr or 0)
        token_map[sym]["rows"].append({
            "exchange": row.slug,
            "exchange_name": row.name,
            "avg_apr": round(apr, 4),
            "max_apr": round(float(row.max_apr or 0), 4),
            "min_apr": round(float(row.min_apr or 0), 4),
            "avg_oi": round(float(row.avg_oi or 0), 2),
            "avg_vol": round(float(row.avg_vol or 0), 2),
            "latest_time": row.latest_time.isoformat() if row.latest_time else None,
        })
        token_map[sym]["max_apr"] = max(token_map[sym]["max_apr"], apr)
        token_map[sym]["min_apr"] = min(token_map[sym]["min_apr"], apr)
        token_map[sym]["exchange_count"] += 1

    ranked = sorted(token_map.values(), key=lambda t: t["max_apr"], reverse=True)
    for t in ranked:
        t["spread_apr"] = round(t["max_apr"] - t["min_apr"], 4)
        if t["min_apr"] == float("inf"):
            t["min_apr"] = 0.0

    total = len(ranked)
    return {
        "data": ranked[offset : offset + limit],
        "total": total,
        "limit": limit,
        "offset": offset,
        "source": "db",
        "timeframe": timeframe,
    }


# ── GET /funding/history/{token} ───────────────────────────────────────────────

@router.get(
    "/history/{token}",
    summary="Historical funding rate time-series for a token",
    response_model=dict,
)
async def get_funding_history(
    token: str,
    exchange: str = Query(..., description="Exchange slug (e.g. hyperliquid)"),
    timeframe: str = Query("24h", description="24h | 3d | 7d | 15d | 31d"),
    interval: str = Query("1h", description="Aggregation bucket: 1h | 4h | 8h | 1d"),
    _rl: None = Depends(rate_limit),
    tier: str = Depends(get_current_user_tier),
) -> Any:
    """
    Returns a time-series of funding APR for **{token}** on a specific exchange.

    Uses TimescaleDB `time_bucket` for efficient aggregation.
    """
    _timeframe_to_delta(timeframe)

    # Map interval string to PostgreSQL interval
    _interval_map = {"1h": "1 hour", "4h": "4 hours", "8h": "8 hours", "1d": "1 day"}
    pg_interval = _interval_map.get(interval)
    if pg_interval is None:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid interval '{interval}'. Valid: {list(_interval_map)}",
        )

    delta = _timeframe_to_delta(timeframe)
    since = datetime.now(tz=timezone.utc) - delta

    async with get_db_session() as session:
        # Verify token and exchange exist
        token_row = await session.scalar(
            select(Token.id).where(Token.symbol == token.upper())
        )
        if token_row is None:
            raise HTTPException(status_code=404, detail=f"Token '{token}' not found.")

        exchange_row = await session.scalar(
            select(Exchange.id).where(Exchange.slug == exchange.lower())
        )
        if exchange_row is None:
            raise HTTPException(
                status_code=404, detail=f"Exchange '{exchange}' not found."
            )

        # Use time_bucket (TimescaleDB function) for efficient downsampling
        q = text(
            """
            SELECT
                time_bucket(:interval, time) AS bucket,
                AVG(funding_apr)             AS avg_apr,
                AVG(funding_rate_8h)         AS avg_rate_8h,
                AVG(mark_price)              AS avg_mark_price,
                AVG(open_interest_usd)       AS avg_oi,
                COUNT(*)                     AS sample_count
            FROM funding_rates
            WHERE
                token_id    = :token_id
                AND exchange_id = :exchange_id
                AND time        >= :since
            GROUP BY bucket
            ORDER BY bucket ASC
            """
        )
        result = await session.execute(
            q,
            {
                "interval": pg_interval,
                "token_id": token_row,
                "exchange_id": exchange_row,
                "since": since,
            },
        )
        rows = result.all()

    data = [
        {
            "time": row.bucket.isoformat(),
            "avg_apr": round(float(row.avg_apr or 0), 6),
            "avg_rate_8h": round(float(row.avg_rate_8h or 0), 8),
            "avg_mark_price": round(float(row.avg_mark_price or 0), 4),
            "avg_oi": round(float(row.avg_oi or 0), 2),
            "sample_count": row.sample_count,
        }
        for row in rows
    ]

    return {
        "token": token.upper(),
        "exchange": exchange.lower(),
        "timeframe": timeframe,
        "interval": interval,
        "data": data,
        "count": len(data),
    }


# ── GET /funding/token/{token} ─────────────────────────────────────────────────

@router.get(
    "/token/{token}",
    summary="Full token detail: live snapshot + recent history + arb summary",
    response_model=dict,
)
async def get_token_detail(
    token: str,
    _rl: None = Depends(rate_limit),
    tier: str = Depends(get_current_user_tier),
    redis: Redis = Depends(get_redis_client),
) -> Any:
    """
    Returns a full detail view for **{token}** — suitable for a detail page:

    - `live_snapshot`: current funding rates on each exchange (from Redis)
    - `arb_summary`: best long/short pair from `arbitrage:current`
    - `history_24h`: hourly funding APR for the last 24h from TimescaleDB
    """
    token_upper = token.upper()

    # — Live snapshot from Redis "funding:ranked" —
    snapshot: dict | None = None
    raw_ranked = await redis.get("funding:ranked")
    if raw_ranked:
        ranked = json.loads(raw_ranked)
        for item in ranked:
            if item.get("token") == token_upper:
                snapshot = item
                break

    # — Arbitrage summary from Redis "arbitrage:current" —
    arb_match: dict | None = None
    raw_arb = await redis.get("arbitrage:current")
    if raw_arb:
        arb_list = json.loads(raw_arb)
        for opp in arb_list:
            if opp.get("token") == token_upper:
                arb_match = opp
                break

    # — 24h hourly history from TimescaleDB —
    since_24h = datetime.now(tz=timezone.utc) - timedelta(hours=24)
    history_data: list[dict] = []
    async with get_db_session() as session:
        token_row = await session.scalar(
            select(Token.id).where(Token.symbol == token_upper)
        )
        if token_row:
            q = text(
                """
                SELECT
                    time_bucket('1 hour', time)  AS bucket,
                    e.slug                       AS exchange,
                    AVG(funding_apr)             AS avg_apr,
                    AVG(mark_price)              AS avg_price
                FROM funding_rates fr
                JOIN exchanges e ON e.id = fr.exchange_id
                WHERE fr.token_id = :token_id AND fr.time >= :since
                GROUP BY bucket, e.slug
                ORDER BY bucket ASC, e.slug
                """
            )
            result = await session.execute(
                q, {"token_id": token_row, "since": since_24h}
            )
            history_data = [
                {
                    "time": row.bucket.isoformat(),
                    "exchange": row.exchange,
                    "avg_apr": round(float(row.avg_apr or 0), 6),
                    "avg_price": round(float(row.avg_price or 0), 4),
                }
                for row in result.all()
            ]

    if snapshot is None and not history_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No data found for token '{token_upper}'.",
        )

    return {
        "token": token_upper,
        "live_snapshot": snapshot,
        "arb_summary": arb_match,
        "history_24h": history_data,
    }
