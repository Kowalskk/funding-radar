"""
app/services/funding_service.py — Orchestrates the full data pipeline.

                ┌─────────────────────────────────────────────────┐
                │  Redis channel: "funding:updates"               │
                │  (published by HyperliquidCollector,            │
                │   AsterCollector, …)                            │
                └────────────────────┬────────────────────────────┘
                                     │ subscribe (async generator)
                                     ▼
                          ┌──────────────────┐
                          │  DataNormalizer  │  (in-memory state)
                          └────────┬─────────┘
                               after every N updates or T seconds
                                     │
                     ┌───────────────┴────────────────┐
                     ▼                                ▼
          ┌──────────────────────┐       ┌─────────────────────────┐
          │ ArbitrageCalculator  │       │   FundingAggregator     │
          │ → "arbitrage:current"│       │   → "funding:ranked"    │
          │ → "arbitrage:updates"│       │   → "funding:ranked:…"  │
          └──────────────────────┘       └─────────────────────────┘
                                     │
                   every `db_persist_interval` seconds
                                     ▼
                         ┌────────────────────┐
                         │  TimescaleDB write  │
                         │  (funding_rates)   │
                         └────────────────────┘

Staleness policy:
  - Snapshots older than `stale_after_seconds` are excluded from arbitrage
    and rankings, but kept in the normalizer for diagnostics.
  - A housekeeping task purges stale snapshots every `purge_interval` seconds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base import NormalizedFundingData
from app.core.database import get_db_session
from app.core.redis import get_redis
from app.processors.arbitrage_calculator import ArbitrageCalculator
from app.processors.funding_aggregator import FundingAggregator
from app.processors.normalizer import DataNormalizer

logger = logging.getLogger(__name__)


class FundingService:
    """Subscribes to 'funding:updates', drives processors, persists to DB."""

    def __init__(
        self,
        redis: Redis,
        *,
        stale_after_seconds: float = 120.0,
        recalculate_every_n: int = 5,        # recalc after every N updates
        recalculate_every_seconds: float = 5.0,  # or every 5s, whichever comes first
        db_persist_interval: float = 30.0,   # write snapshots to DB every 30s
        purge_interval: float = 300.0,       # purge stale entries every 5 min
        min_net_apr_taker: float = 0.0,      # exclude arb below this APR
    ) -> None:
        self._redis = redis
        self._running = False
        self._tasks: list[asyncio.Task] = []

        self.normalizer = DataNormalizer(stale_after_seconds=stale_after_seconds)
        self.arb_calculator = ArbitrageCalculator(
            redis=redis,
            normalizer=self.normalizer,
            min_net_apr_taker=min_net_apr_taker,
        )
        self.aggregator = FundingAggregator(redis=redis, normalizer=self.normalizer)

        self._recalculate_every_n = recalculate_every_n
        self._recalculate_every_seconds = recalculate_every_seconds
        self._db_persist_interval = db_persist_interval
        self._purge_interval = purge_interval

        self._update_counter: int = 0
        self._last_recalc_at: float = 0.0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            logger.warning("FundingService already running.")
            return

        self._running = True
        logger.info("FundingService starting…")

        self._tasks = [
            asyncio.create_task(self._subscriber_loop(), name="funding_service:sub"),
            asyncio.create_task(self._timed_recalc_loop(), name="funding_service:recalc"),
            asyncio.create_task(self._db_persist_loop(), name="funding_service:db"),
            asyncio.create_task(self._purge_loop(), name="funding_service:purge"),
        ]
        logger.info("FundingService started (%d tasks).", len(self._tasks))

    async def stop(self) -> None:
        if not self._running:
            return
        logger.info("FundingService stopping…")
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("FundingService stopped.")

    # ── Subscriber ────────────────────────────────────────────────────────────

    async def _subscriber_loop(self) -> None:
        """Subscribe to 'funding:updates' and feed updates into the normalizer."""
        while self._running:
            pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
            try:
                await pubsub.subscribe("funding:updates")
                logger.info("Subscribed to Redis channel 'funding:updates'.")

                async for message in pubsub.listen():
                    if not self._running:
                        break
                    if not message or message.get("type") != "message":
                        continue

                    await self._handle_message(message["data"])

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._running:
                    break
                logger.error(
                    "Subscriber error, reconnecting in 3s: %s", exc, exc_info=True
                )
                await asyncio.sleep(3)
            finally:
                try:
                    await pubsub.unsubscribe("funding:updates")
                    await pubsub.aclose()
                except Exception:
                    pass

    async def _handle_message(self, raw: str | bytes) -> None:
        """Deserialise one NormalizedFundingData message and update state."""
        try:
            data_dict = json.loads(raw)
            data = NormalizedFundingData(**data_dict)
            self.normalizer.update(data)
            self._update_counter += 1

            # Trigger recalculation after N updates
            if self._update_counter % self._recalculate_every_n == 0:
                await self._recalculate()

        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.warning("Could not parse funding update: %s", exc)

    # ── Timed recalculation ───────────────────────────────────────────────────

    async def _timed_recalc_loop(self) -> None:
        """Recalculate and publish even if the subscriber is quiet."""
        while self._running:
            await asyncio.sleep(self._recalculate_every_seconds)
            now = time.monotonic()
            if now - self._last_recalc_at >= self._recalculate_every_seconds:
                await self._recalculate()

    async def _recalculate(self) -> None:
        """Run both calculators and publish their results to Redis."""
        try:
            self._last_recalc_at = time.monotonic()
            arb_count = await self.arb_calculator.calculate_and_publish()
            ranked_count = await self.aggregator.build_and_publish()
            logger.debug(
                "Recalculation done: %d arb pairs, %d tokens ranked.",
                arb_count,
                ranked_count,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Recalculation error: %s", exc, exc_info=True)

    # ── DB persistence ────────────────────────────────────────────────────────

    async def _db_persist_loop(self) -> None:
        """Periodically write current snapshots to TimescaleDB."""
        # Small offset so the first persist happens after collectors warm up
        await asyncio.sleep(self._db_persist_interval)
        while self._running:
            try:
                await self._persist_to_db()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("DB persist error: %s", exc, exc_info=True)
            await asyncio.sleep(self._db_persist_interval)

    async def _persist_to_db(self) -> None:
        """Write all current snapshots to the funding_rates TimescaleDB hypertable."""
        snapshots = self.normalizer.get_all_latest()
        if not snapshots:
            return

        from datetime import datetime, timezone
        from app.models.db.exchange import Exchange
        from app.models.db.token import Token
        from app.models.db.funding_rate import FundingRate
        from sqlalchemy import select

        inserted = 0
        async with get_db_session() as session:
            # Build exchange and token lookup tables (cached per-call)
            exchanges = {
                e.slug: e.id
                for e in (await session.execute(select(Exchange))).scalars().all()
            }
            tokens = {
                t.symbol: t.id
                for t in (await session.execute(select(Token))).scalars().all()
            }

            for data in snapshots:
                exchange_id = exchanges.get(data.exchange)
                token_id = tokens.get(data.token)

                if exchange_id is None or token_id is None:
                    # Exchange or token not yet in DB — skip silently
                    continue

                ts = datetime.fromtimestamp(data.timestamp / 1000, tz=timezone.utc)
                record = FundingRate(
                    time=ts,
                    exchange_id=exchange_id,
                    token_id=token_id,
                    funding_rate=Decimal(str(data.funding_rate)),
                    funding_rate_8h=Decimal(str(data.funding_rate_8h)),
                    funding_apr=Decimal(str(data.funding_apr)),
                    mark_price=Decimal(str(data.mark_price)),
                    index_price=Decimal(str(data.index_price)),
                    open_interest_usd=Decimal(str(data.open_interest_usd)),
                    volume_24h_usd=Decimal(str(data.volume_24h_usd)),
                    price_spread_pct=Decimal(str(data.price_spread_pct)),
                )
                # merge avoids duplicate PK errors on re-insert of same timestamp
                await session.merge(record)
                inserted += 1

        logger.info("Persisted %d/%d funding snapshots to DB.", inserted, len(snapshots))

    # ── Stale purge ───────────────────────────────────────────────────────────

    async def _purge_loop(self) -> None:
        """Remove stale normalizer entries every `purge_interval` seconds."""
        while self._running:
            await asyncio.sleep(self._purge_interval)
            try:
                purged = self.normalizer.purge_stale()
                if purged:
                    logger.info("Purged %d stale snapshots from normalizer.", purged)
            except Exception as exc:
                logger.error("Purge error: %s", exc)

    # ── Status ────────────────────────────────────────────────────────────────

    @property
    def status(self) -> dict:
        return {
            "running": self._running,
            "update_count": self._update_counter,
            "normalizer": self.normalizer.stats,
            "last_recalc_ago_s": round(time.monotonic() - self._last_recalc_at, 1),
            "tasks_alive": sum(1 for t in self._tasks if not t.done()),
        }
