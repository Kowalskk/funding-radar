#!/usr/bin/env python
"""
scripts/seed_exchanges.py — Insert or update exchange and token seed data.

Run inside the app container:
  docker compose exec app python scripts/seed_exchanges.py
"""

from __future__ import annotations

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


EXCHANGES = [
    {
        "slug": "hyperliquid",
        "name": "Hyperliquid",
        "logo_url": "https://hyperliquid.xyz/logo.png",
        "maker_fee": 0.01,
        "taker_fee": 0.035,
        "funding_interval_hours": 1,
        "is_active": True,
    },
    {
        "slug": "aster",
        "name": "AsterDEX",
        "logo_url": "https://asterdex.com/logo.png",
        "maker_fee": 0.01,
        "taker_fee": 0.035,
        "funding_interval_hours": 8,
        "is_active": True,
    },
]

TOKENS = [
    {"symbol": "BTC", "name": "Bitcoin", "is_active": True},
    {"symbol": "ETH", "name": "Ethereum", "is_active": True},
    {"symbol": "SOL", "name": "Solana", "is_active": True},
    {"symbol": "ARB", "name": "Arbitrum", "is_active": True},
    {"symbol": "AVAX", "name": "Avalanche", "is_active": True},
    {"symbol": "MATIC", "name": "Polygon", "is_active": True},
    {"symbol": "DOGE", "name": "Dogecoin", "is_active": True},
    {"symbol": "LINK", "name": "Chainlink", "is_active": True},
    {"symbol": "OP", "name": "Optimism", "is_active": True},
    {"symbol": "SUI", "name": "Sui", "is_active": True},
    {"symbol": "APT", "name": "Aptos", "is_active": True},
    {"symbol": "SEI", "name": "Sei", "is_active": True},
    {"symbol": "TIA", "name": "Celestia", "is_active": True},
    {"symbol": "INJ", "name": "Injective", "is_active": True},
    {"symbol": "PEPE", "name": "Pepe", "is_active": True},
    {"symbol": "WIF", "name": "dogwifhat", "is_active": True},
    {"symbol": "BONK", "name": "Bonk", "is_active": True},
    {"symbol": "JTO", "name": "Jito", "is_active": True},
]

# Tokens available on each exchange
EXCHANGE_TOKENS = {
    "hyperliquid": ["BTC", "ETH", "SOL", "ARB", "AVAX", "MATIC", "DOGE",
                    "LINK", "OP", "SUI", "APT", "SEI", "TIA", "INJ", "PEPE",
                    "WIF", "BONK", "JTO"],
    "aster":       ["BTC", "ETH", "SOL", "ARB", "AVAX", "DOGE", "LINK",
                    "OP", "SUI", "PEPE", "WIF"],
}


async def main() -> None:
    from app.config import get_settings
    from app.core.database import init_db, get_db_session
    from app.models.db.exchange import Exchange
    from app.models.db.token import ExchangeToken, Token
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    settings = get_settings()
    await init_db(settings)

    async with get_db_session() as session:
        print("Seeding exchanges…")
        for ex_data in EXCHANGES:
            stmt = insert(Exchange).values(**ex_data).on_conflict_do_update(
                index_elements=["slug"], set_=ex_data
            )
            await session.execute(stmt)
        await session.flush()

        print("Seeding tokens…")
        for tok_data in TOKENS:
            stmt = insert(Token).values(**tok_data).on_conflict_do_update(
                index_elements=["symbol"], set_=tok_data
            )
            await session.execute(stmt)
        await session.flush()

        print("Seeding exchange_tokens…")
        for ex_slug, token_symbols in EXCHANGE_TOKENS.items():
            ex = await session.scalar(select(Exchange).where(Exchange.slug == ex_slug))
            if ex is None:
                print(f"  WARNING: exchange '{ex_slug}' not found — skipping.")
                continue
            for sym in token_symbols:
                tok = await session.scalar(select(Token).where(Token.symbol == sym))
                if tok is None:
                    continue
                stmt = insert(ExchangeToken).values(
                    exchange_id=ex.id, token_id=tok.id, exchange_symbol=f"{sym}USDT"
                ).on_conflict_do_nothing()
                await session.execute(stmt)

        await session.commit()
        print("✅ Seed complete.")


if __name__ == "__main__":
    asyncio.run(main())
