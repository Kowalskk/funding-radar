"""
app/processors/apr_windows.py — Multi-timeframe APR window calculator.

Computes average funding APR for time windows (1H, 8H, 24H, 7D, 30D)
from the TimescaleDB funding_rates hypertable using a single optimized
SQL query with FILTER aggregation.

Results are cached in Redis for 60 seconds to avoid hammering the DB.

Usage:
    helper = APRWindowHelper(redis)
    windows = await helper.get_windows("hyperliquid", "BTC")
    # → {"apr_1h": -12.4, "apr_8h": -10.2, "apr_24h": -8.6,
    #    "apr_7d": -5.1, "apr_30d": -3.8, "data_points_30d": 719}

    # For a specific pair (returns per-leg + net windows):
    pair_windows = await helper.get_pair_windows("hyperliquid", "aster", "BTC")
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text

from app.core.database import get_db_session

logger = logging.getLogger(__name__)

# Window definitions: (label, SQL interval string)
_WINDOWS: list[tuple[str, str]] = [
    ("apr_1h",  "1 hour"),
    ("apr_8h",  "8 hours"),
    ("apr_24h", "24 hours"),
    ("apr_7d",  "7 days"),
    ("apr_30d", "30 days"),
]

_CACHE_TTL = 60   # seconds


class APRWindowHelper:
    """Computes and caches multi-timeframe APR windows from TimescaleDB."""

    def __init__(self, redis) -> None:
        self._redis = redis

    async def get_windows(
        self,
        exchange_slug: str,
        token_symbol: str,
    ) -> dict[str, Any]:
        """Return APR windows for a single (exchange, token) pair.

        Returns None for windows with insufficient data.
        Always looks back 30 days maximum.
        """
        cache_key = f"apr_windows:{exchange_slug}:{token_symbol.upper()}"
        cached = await self._redis.get(cache_key)
        if cached:
            return json.loads(cached)

        result = await self._query_windows(exchange_slug, token_symbol)
        await self._redis.set(cache_key, json.dumps(result), ex=_CACHE_TTL)
        return result

    async def get_pair_windows(
        self,
        long_exchange: str,
        short_exchange: str,
        token: str,
    ) -> dict[str, Any]:
        """Return per-leg and net APR windows for an arbitrage pair."""
        long_w, short_w = await _parallel_gather(
            self.get_windows(long_exchange, token),
            self.get_windows(short_exchange, token),
        )

        net: dict[str, Any] = {}
        for key in ("apr_1h", "apr_8h", "apr_24h", "apr_7d", "apr_30d"):
            long_v = long_w.get(key)
            short_v = short_w.get(key)
            # Net = short_apr - long_apr (short gets paid, long pays)
            if long_v is not None and short_v is not None:
                net[f"net_{key}"] = round(short_v - long_v, 4)
            else:
                net[f"net_{key}"] = None

        return {
            "long_windows": long_w,
            "short_windows": short_w,
            **net,
        }

    async def get_batch_windows(
        self,
        pairs: list[tuple[str, str]],   # [(exchange_slug, token_symbol), ...]
    ) -> dict[str, dict[str, Any]]:
        """Fetch windows for many (exchange, token) pairs in one call.

        Returns {f"{exchange}:{token}": windows_dict, ...}
        """
        tasks = [self.get_windows(ex, tok) for ex, tok in pairs]
        results = await _parallel_gather(*tasks)
        return {
            f"{ex}:{tok}": res
            for (ex, tok), res in zip(pairs, results)
        }

    # ── SQL query ─────────────────────────────────────────────────────────────

    async def _query_windows(
        self, exchange_slug: str, token_symbol: str
    ) -> dict[str, Any]:
        """Single SQL query using FILTER aggregation — one round-trip to the DB."""
        sql = text("""
            SELECT
                AVG(fr.funding_apr) FILTER (
                    WHERE fr.time >= NOW() - INTERVAL '1 hour'
                )::float  AS apr_1h,
                AVG(fr.funding_apr) FILTER (
                    WHERE fr.time >= NOW() - INTERVAL '8 hours'
                )::float  AS apr_8h,
                AVG(fr.funding_apr) FILTER (
                    WHERE fr.time >= NOW() - INTERVAL '24 hours'
                )::float  AS apr_24h,
                AVG(fr.funding_apr) FILTER (
                    WHERE fr.time >= NOW() - INTERVAL '7 days'
                )::float  AS apr_7d,
                AVG(fr.funding_apr) FILTER (
                    WHERE fr.time >= NOW() - INTERVAL '30 days'
                )::float  AS apr_30d,
                COUNT(*) FILTER (
                    WHERE fr.time >= NOW() - INTERVAL '30 days'
                )         AS data_points_30d
            FROM funding_rates fr
            JOIN exchanges ex ON ex.id = fr.exchange_id
            JOIN tokens    tk ON tk.id = fr.token_id
            WHERE ex.slug     = :exchange_slug
              AND tk.symbol   = :token_symbol
              AND fr.time    >= NOW() - INTERVAL '30 days'
        """)

        try:
            async with get_db_session() as session:
                row = (await session.execute(
                    sql,
                    {"exchange_slug": exchange_slug, "token_symbol": token_symbol.upper()},
                )).one_or_none()

            if row is None:
                return _empty_windows()

            return {
                "apr_1h":          _round(row.apr_1h),
                "apr_8h":          _round(row.apr_8h),
                "apr_24h":         _round(row.apr_24h),
                "apr_7d":          _round(row.apr_7d),
                "apr_30d":         _round(row.apr_30d),
                "data_points_30d": int(row.data_points_30d or 0),
            }
        except Exception as exc:
            logger.warning(
                "APR window query failed for %s/%s: %s",
                exchange_slug, token_symbol, exc
            )
            return _empty_windows()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _round(value) -> float | None:
    if value is None:
        return None
    return round(float(value), 4)


def _empty_windows() -> dict[str, Any]:
    return {
        "apr_1h": None, "apr_8h": None, "apr_24h": None,
        "apr_7d": None, "apr_30d": None, "data_points_30d": 0,
    }


async def _parallel_gather(*coros):
    import asyncio
    return await asyncio.gather(*coros)
