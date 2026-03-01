"""
app/processors/funding_aggregator.py — Aggregates funding rates across exchanges.

Maintains a ranked list of tokens sorted by their maximum APR across all exchanges.
Publishes to:
  - Redis key     "funding:ranked"         (JSON, full list with TTL)
  - Redis channel "funding:ranked:updates" (pub/sub for WebSocket consumers)

Also provides a per-exchange view and a per-token cross-exchange view for the API.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass

from redis.asyncio import Redis

from app.collectors.base import NormalizedFundingData
from app.processors.normalizer import DataNormalizer, ExchangeSnapshot

logger = logging.getLogger(__name__)

REDIS_KEY_TTL = 120  # seconds


@dataclass
class ExchangeRateRow:
    """A single row in the funding rate table: one token on one exchange."""

    exchange: str
    token: str
    funding_rate: float
    funding_rate_8h: float
    funding_apr: float
    mark_price: float
    open_interest_usd: float
    volume_24h_usd: float
    price_spread_pct: float
    maker_fee: float
    taker_fee: float
    is_live: bool
    is_stale: bool
    age_seconds: float
    timestamp: int


@dataclass
class TokenRankRow:
    """Cross-exchange summary for a single token."""

    token: str
    max_apr: float          # highest APR across all live exchanges
    min_apr: float          # lowest APR across all live exchanges
    spread_apr: float       # max_apr − min_apr (gross arb spread)
    exchange_count: int
    rows: list[ExchangeRateRow]


class FundingAggregator:
    """Builds ranked funding rate tables from DataNormalizer state."""

    def __init__(self, redis: Redis, normalizer: DataNormalizer) -> None:
        self._redis = redis
        self._normalizer = normalizer
        self._last_ranked: list[TokenRankRow] = []

    # ── Core aggregation ──────────────────────────────────────────────────────

    def build_ranked(self) -> list[TokenRankRow]:
        """Produce a list of TokenRankRow sorted by max_apr descending.

        Tokens with no live data are excluded.
        """
        rows: list[TokenRankRow] = []

        for token_view in self._normalizer.iter_tokens():
            live = token_view.live_snapshots()
            if not live:
                continue

            exchange_rows = [self._snap_to_row(s) for s in token_view.all_snapshots()]

            rows.append(
                TokenRankRow(
                    token=token_view.token,
                    max_apr=token_view.max_apr(),
                    min_apr=token_view.min_apr(),
                    spread_apr=token_view.spread_apr(),
                    exchange_count=token_view.exchange_count(),
                    rows=exchange_rows,
                )
            )

        rows.sort(key=lambda r: r.max_apr, reverse=True)
        self._last_ranked = rows
        return rows

    def build_exchange_view(self, exchange: str) -> list[ExchangeRateRow]:
        """All tokens on a single exchange, sorted by funding_apr descending."""
        result: list[ExchangeRateRow] = []
        for token_view in self._normalizer.iter_tokens():
            snap = token_view.snapshots.get(exchange)
            if snap:
                result.append(self._snap_to_row(snap))
        return sorted(result, key=lambda r: r.funding_apr, reverse=True)

    def build_token_view(self, token: str) -> list[ExchangeRateRow] | None:
        """All exchanges for a single token, sorted by funding_apr descending."""
        tv = self._normalizer.get_token_view(token)
        if tv is None:
            return None
        rows = [self._snap_to_row(s) for s in tv.all_snapshots()]
        return sorted(rows, key=lambda r: r.funding_apr, reverse=True)

    # ── Redis publish ─────────────────────────────────────────────────────────

    async def build_and_publish(self) -> int:
        """Build rankings and publish to Redis. Returns number of tokens published."""
        ranked = self.build_ranked()
        payload = json.dumps([self._rank_to_dict(r) for r in ranked])

        try:
            pipe = self._redis.pipeline(transaction=False)
            pipe.set("funding:ranked", payload, ex=REDIS_KEY_TTL)
            pipe.publish("funding:ranked:updates", payload)
            await pipe.execute()
            logger.debug(
                "Published funding rankings: %d tokens.", len(ranked)
            )
        except Exception as exc:
            logger.error("Failed to publish funding rankings: %s", exc)

        return len(ranked)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _snap_to_row(snap: ExchangeSnapshot) -> ExchangeRateRow:
        d = snap.data
        return ExchangeRateRow(
            exchange=d.exchange,
            token=d.token,
            funding_rate=d.funding_rate,
            funding_rate_8h=d.funding_rate_8h,
            funding_apr=d.funding_apr,
            mark_price=d.mark_price,
            open_interest_usd=d.open_interest_usd,
            volume_24h_usd=d.volume_24h_usd,
            price_spread_pct=d.price_spread_pct,
            maker_fee=d.maker_fee,
            taker_fee=d.taker_fee,
            is_live=d.is_live,
            is_stale=snap.is_stale(),
            age_seconds=round(snap.age_seconds, 1),
            timestamp=d.timestamp,
        )

    @staticmethod
    def _rank_to_dict(r: TokenRankRow) -> dict:
        return {
            "token": r.token,
            "max_apr": r.max_apr,
            "min_apr": r.min_apr,
            "spread_apr": r.spread_apr,
            "exchange_count": r.exchange_count,
            "rows": [asdict(row) for row in r.rows],
        }

    # ── Convenience queries ───────────────────────────────────────────────────

    @property
    def last_ranked(self) -> list[TokenRankRow]:
        """Last computed ranking (may be empty before first build_ranked call)."""
        return self._last_ranked

    def top_n(self, n: int = 20) -> list[TokenRankRow]:
        """Return the top N tokens by max APR from the last build."""
        return self._last_ranked[:n]
