#!/usr/bin/env python
"""
scripts/backfill_funding.py — Download historical funding rates and store in TimescaleDB.

Usage:
  docker compose exec app python scripts/backfill_funding.py --days 30
  docker compose exec app python scripts/backfill_funding.py --exchange hyperliquid --days 7
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import os
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


async def backfill_hyperliquid(session, exchange_id: int, token_id: int,
                                asset: str, days: int) -> int:
    import aiohttp
    from app.models.db.funding_rate import FundingRate
    from sqlalchemy.dialects.postgresql import insert

    rest_url = "https://api.hyperliquid.xyz/info"
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 3_600_000

    records_saved = 0
    async with aiohttp.ClientSession() as http:
        async with http.post(rest_url, json={
            "type": "fundingHistory",
            "coin": asset,
            "startTime": start_ms,
        }) as resp:
            data = await resp.json()

    if not isinstance(data, list):
        return 0

    for record in data:
        try:
            ts = datetime.fromtimestamp(int(record["time"]) / 1000, tz=timezone.utc)
            rate = float(record.get("fundingRate", 0))
            funding_rate_8h = rate * 8
            funding_apr = rate * 24 * 365 * 100

            stmt = insert(FundingRate).values(
                time=ts,
                exchange_id=exchange_id,
                token_id=token_id,
                funding_rate=rate,
                funding_rate_8h=funding_rate_8h,
                funding_apr=funding_apr,
                mark_price=float(record.get("premium", 0)) + 1.0,  # placeholder
                index_price=1.0,
                open_interest_usd=0.0,
                volume_24h_usd=0.0,
                price_spread_pct=0.0,
            ).on_conflict_do_nothing()
            await session.execute(stmt)
            records_saved += 1
        except Exception:
            continue

    return records_saved


async def backfill_aster(session, exchange_id: int, token_id: int,
                          symbol: str, days: int) -> int:
    import aiohttp
    from app.models.db.funding_rate import FundingRate
    from sqlalchemy.dialects.postgresql import insert

    base_url = "https://fapi.asterdex.com"
    limit = min(days * 3, 1000)  # 8h periods, max 1000

    records_saved = 0
    async with aiohttp.ClientSession() as http:
        params = {"symbol": symbol, "limit": limit}
        async with http.get(f"{base_url}/fapi/v1/fundingRate", params=params) as resp:
            data = await resp.json()

    if not isinstance(data, list):
        return 0

    for record in data:
        try:
            ts = datetime.fromtimestamp(int(record["fundingTime"]) / 1000, tz=timezone.utc)
            rate = float(record.get("fundingRate", 0))
            funding_rate_8h = rate
            funding_apr = rate * 3 * 365 * 100

            stmt = insert(FundingRate).values(
                time=ts,
                exchange_id=exchange_id,
                token_id=token_id,
                funding_rate=rate,
                funding_rate_8h=funding_rate_8h,
                funding_apr=funding_apr,
                mark_price=float(record.get("markPrice", 0)),
                index_price=0.0,
                open_interest_usd=0.0,
                volume_24h_usd=0.0,
                price_spread_pct=0.0,
            ).on_conflict_do_nothing()
            await session.execute(stmt)
            records_saved += 1
        except Exception:
            continue

    return records_saved


async def main(exchange_filter: str | None, days: int) -> None:
    from app.config import get_settings
    from app.core.database import init_db, get_db_session
    from app.models.db.exchange import Exchange
    from app.models.db.token import ExchangeToken, Token
    from sqlalchemy import select

    settings = get_settings()
    await init_db(settings)

    async with get_db_session() as session:
        exchanges_q = select(Exchange).where(Exchange.is_active.is_(True))
        if exchange_filter:
            exchanges_q = exchanges_q.where(Exchange.slug == exchange_filter)
        exchanges = (await session.scalars(exchanges_q)).all()

        total = 0
        for ex in exchanges:
            print(f"\n📥 Backfilling {ex.name} ({days} days)…")
            et_rows = (await session.scalars(
                select(ExchangeToken).where(ExchangeToken.exchange_id == ex.id)
            )).all()

            for et in et_rows:
                tok = await session.get(Token, et.token_id)
                if not tok:
                    continue
                print(f"  {ex.slug}/{tok.symbol}…", end=" ", flush=True)
                try:
                    if ex.slug == "hyperliquid":
                        n = await backfill_hyperliquid(session, ex.id, tok.id, tok.symbol, days)
                    elif ex.slug == "aster":
                        n = await backfill_aster(session, ex.id, tok.id, et.exchange_symbol, days)
                    else:
                        n = 0
                    await session.commit()
                    print(f"{n} records")
                    total += n
                    await asyncio.sleep(0.5)  # rate limiting
                except Exception as e:
                    print(f"ERROR: {e}")
                    await session.rollback()

        print(f"\n✅ Backfill complete: {total} records saved.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill historical funding rates.")
    parser.add_argument("--exchange", default=None, help="Exchange slug (default: all)")
    parser.add_argument("--days", type=int, default=30, help="Days of history to fetch")
    args = parser.parse_args()
    asyncio.run(main(args.exchange, args.days))
