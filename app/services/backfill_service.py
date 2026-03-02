"""
app/services/backfill_service.py — Historical funding rate backfill.

Responsible for populating `funding_rates` with historical data when a
collector first starts up (or when the DB is empty). Works with ANY
exchange collector that implements `_fetch_history_range()`.

Architecture:
  BackfillService.run_all(collectors)
    → for each collector: BackfillService.backfill(collector, days=30)
        → async for record in collector._fetch_history_range(start, end)
        → upsert into funding_rates via TimescaleDB ON CONFLICT DO NOTHING
        → auto-creates exchange/token rows if they don't exist yet

Guard: a Redis key `backfill:last:{slug}:{days}` stores the last run
timestamp. The backfill is skipped if it was run in the last 23 hours,
preventing repeated backfills on container restarts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.database import get_db_session
from app.models.db.exchange import Exchange
from app.models.db.funding_rate import FundingRate
from app.models.db.token import Token

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from app.collectors.base import BaseCollector

logger = logging.getLogger(__name__)

# Skip backfill if it ran less than this many seconds ago
_GUARD_SECONDS = 23 * 3600  # 23 hours
_BATCH_SIZE = 500            # rows per DB upsert batch


class BackfillService:
    """Back-populates the funding_rates TimescaleDB hypertable from all collectors."""

    def __init__(self, redis: "Redis") -> None:
        self._redis = redis

    # ── Public API ────────────────────────────────────────────────────────────

    async def run_all(
        self,
        collectors: list["BaseCollector"],
        days: int = 30,
    ) -> None:
        """Run backfill for every collector in parallel (up to 3 at once)."""
        semaphore = asyncio.Semaphore(3)

        async def _guarded(collector: "BaseCollector") -> None:
            async with semaphore:
                await self.backfill(collector, days=days)

        tasks = [asyncio.create_task(_guarded(c)) for c in collectors]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for collector, result in zip(collectors, results):
            if isinstance(result, Exception):
                logger.error(
                    "Backfill failed for %s: %s",
                    collector.exchange_slug, result, exc_info=result
                )

    async def backfill(
        self,
        collector: "BaseCollector",
        days: int = 30,
    ) -> int:
        """Backfill one exchange. Returns the number of rows inserted.

        Skips if the backfill was run within the last 23 hours.
        Auto-creates exchange and token rows if they don't exist.
        """
        slug = collector.exchange_slug
        guard_key = f"backfill:last:{slug}:{days}d"

        # --- Guard: skip if recently run ---
        last_run = await self._redis.get(guard_key)
        if last_run:
            ago = time.time() - float(last_run)
            if ago < _GUARD_SECONDS:
                logger.info(
                    "Skipping backfill for %s (last run %.1fh ago).", slug, ago / 3600
                )
                return 0

        now_ms = int(time.time() * 1000)
        start_ms = now_ms - days * 24 * 3_600_000
        logger.info(
            "Starting %d-day backfill for %s (from %s)…",
            days, slug,
            datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
        )

        # --- Ensure exchange row exists ---
        exchange_id = await self._ensure_exchange(slug, collector)
        if exchange_id is None:
            logger.error("Could not find/create exchange row for %s — aborting.", slug)
            return 0

        inserted = 0
        batch: list[dict] = []

        async for record in collector._fetch_history_range(start_ms, now_ms):
            token_id = await self._ensure_token(record.token)
            if token_id is None:
                continue

            ts = datetime.fromtimestamp(record.timestamp / 1000, tz=timezone.utc)
            batch.append({
                "time": ts,
                "exchange_id": exchange_id,
                "token_id": token_id,
                "funding_rate": Decimal(str(record.funding_rate)),
                "funding_rate_8h": Decimal(str(record.funding_rate_8h)),
                "funding_apr": Decimal(str(record.funding_apr)),
                "mark_price": Decimal(str(record.mark_price)) if record.mark_price else None,
                "index_price": Decimal(str(record.index_price)) if record.index_price else None,
                "open_interest_usd": None,
                "volume_24h_usd": None,
                "price_spread_pct": None,
            })

            if len(batch) >= _BATCH_SIZE:
                inserted += await self._upsert_batch(batch)
                batch.clear()

        if batch:
            inserted += await self._upsert_batch(batch)

        # Record guard timestamp
        await self._redis.set(guard_key, str(time.time()), ex=_GUARD_SECONDS)
        logger.info("Backfill complete for %s: %d rows upserted.", slug, inserted)
        return inserted

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _upsert_batch(self, rows: list[dict]) -> int:
        """Insert rows into funding_rates, ignoring conflicts (same PK = same ts)."""
        if not rows:
            return 0
        async with get_db_session() as session:
            stmt = (
                pg_insert(FundingRate)
                .values(rows)
                .on_conflict_do_nothing()
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount or 0

    # ── Exchange / Token auto-creation ────────────────────────────────────────

    _exchange_cache: dict[str, int] = {}
    _token_cache: dict[str, int] = {}

    async def _ensure_exchange(self, slug: str, collector: "BaseCollector") -> int | None:
        if slug in self._exchange_cache:
            return self._exchange_cache[slug]
        async with get_db_session() as session:
            row = (await session.execute(
                select(Exchange).where(Exchange.slug == slug)
            )).scalar_one_or_none()
            if row is None:
                row = Exchange(
                    slug=slug,
                    name=slug.capitalize(),
                    maker_fee=getattr(collector, "maker_fee", 0),
                    taker_fee=getattr(collector, "taker_fee", 0),
                    funding_interval_hours=getattr(collector, "funding_interval_hours", 8),
                    is_active=True,
                )
                session.add(row)
                await session.commit()
                await session.refresh(row)
                logger.info("Auto-created exchange row for '%s'.", slug)
            self._exchange_cache[slug] = row.id
            return row.id

    async def _ensure_token(self, symbol: str) -> int | None:
        if symbol in self._token_cache:
            return self._token_cache[symbol]
        async with get_db_session() as session:
            row = (await session.execute(
                select(Token).where(Token.symbol == symbol.upper())
            )).scalar_one_or_none()
            if row is None:
                row = Token(symbol=symbol.upper(), name=symbol.upper(), is_active=True)
                session.add(row)
                try:
                    await session.commit()
                    await session.refresh(row)
                except Exception:
                    await session.rollback()
                    # Another coroutine may have inserted concurrently
                    row = (await session.execute(
                        select(Token).where(Token.symbol == symbol.upper())
                    )).scalar_one_or_none()
                    if row is None:
                        return None
            self._token_cache[symbol] = row.id
            return row.id
