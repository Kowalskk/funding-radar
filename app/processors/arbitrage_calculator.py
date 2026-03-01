"""
app/processors/arbitrage_calculator.py — Cross-exchange delta-neutral arbitrage engine.

For each token that appears on 2+ exchanges, this calculator:
  1. Identifies the best long leg (lowest / most negative funding APR — you collect)
  2. Identifies the best short leg (highest / most positive funding APR — you pay less)
  3. Computes gross spread, price spread, net APR after fees, and break-even hours
  4. Publishes results to Redis:
       - Key  "arbitrage:current"     (JSON list, overwritten on every run)
       - Channel "arbitrage:updates"  (pub/sub for WebSocket consumers)

The calculator is stateless — it reads from the DataNormalizer and writes to Redis.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any

from redis.asyncio import Redis

from app.collectors.base import NormalizedFundingData
from app.processors.apr_calculator import calculate_breakeven_hours, entry_exit_fee_pct
from app.processors.normalizer import DataNormalizer, ExchangeSnapshot

logger = logging.getLogger(__name__)

# How long the Redis key is valid (seconds); refreshed on every calculation
REDIS_KEY_TTL = 120


@dataclass
class ArbitrageLeg:
    exchange: str
    token: str
    side: str                       # "long" | "short"
    funding_rate: float
    funding_rate_8h: float
    funding_apr: float
    mark_price: float
    index_price: float
    open_interest_usd: float
    maker_fee: float
    taker_fee: float
    is_stale: bool


@dataclass
class ArbitrageResult:
    token: str
    long_leg: ArbitrageLeg          # lowest APR → you earn / pay least
    short_leg: ArbitrageLeg         # highest APR → counterparty pays you

    # Spread metrics
    funding_delta_apr: float        # abs(short_apr - long_apr)
    price_spread_pct: float         # abs(mark_a - mark_b) / avg * 100

    # Net profitability
    net_apr_maker: float            # after maker fee costs (annualised)
    net_apr_taker: float            # after taker fee costs (annualised)
    entry_fee_pct_taker: float      # combined one-way entry cost (%)

    # Break-even
    breakeven_hours_taker: float | None

    detected_at: int                # unix ms


class ArbitrageCalculator:
    """Computes best arbitrage pairs from DataNormalizer state."""

    def __init__(
        self,
        redis: Redis,
        normalizer: DataNormalizer,
        min_net_apr_taker: float = 0.0,    # filter below this threshold
    ) -> None:
        self._redis = redis
        self._normalizer = normalizer
        self._min_net_apr_taker = min_net_apr_taker
        self._last_results: list[ArbitrageResult] = []

    # ── Core calculation ──────────────────────────────────────────────────────

    def calculate(self) -> list[ArbitrageResult]:
        """Compute arbitrage opportunities from current normalizer state.

        Returns a list of ArbitrageResult sorted by net_apr_taker descending.
        """
        results: list[ArbitrageResult] = []
        now_ms = int(time.time() * 1000)

        for token_view in self._normalizer.arbitrage_candidates():
            snapshots = token_view.all_snapshots()
            if len(snapshots) < 2:
                continue

            # Sort by funding APR: index 0 = lowest (long candidate), -1 = highest (short)
            sorted_snaps = sorted(snapshots, key=lambda s: s.data.funding_apr)
            long_snap = sorted_snaps[0]
            short_snap = sorted_snaps[-1]

            result = self._compute_pair(long_snap, short_snap, now_ms)
            if result is None:
                continue

            if result.net_apr_taker >= self._min_net_apr_taker:
                results.append(result)

        results.sort(key=lambda r: r.net_apr_taker, reverse=True)
        self._last_results = results
        return results

    def _compute_pair(
        self,
        long_snap: ExchangeSnapshot,
        short_snap: ExchangeSnapshot,
        now_ms: int,
    ) -> ArbitrageResult | None:
        """Compute a single long/short pair.  Returns None on bad data."""
        try:
            ld = long_snap.data
            sd = short_snap.data

            if ld.exchange == sd.exchange:
                return None  # same exchange — not a valid arb

            # ── Spread ────────────────────────────────────────────────────────
            funding_delta_apr = abs(sd.funding_apr - ld.funding_apr)

            # Price spread between the two legs (slippage risk proxy)
            avg_price = (ld.mark_price + sd.mark_price) / 2
            price_spread_pct = (
                abs(ld.mark_price - sd.mark_price) / avg_price * 100
                if avg_price > 0
                else 0.0
            )

            # ── Net APR — fees are one-time costs (entry + exit), NOT recurring ──
            # Each leg incurs a fee on entry and on exit.
            # Round-trip cost per leg = fee_rate × 2 (open + close)
            # Total round-trip cost across both legs:
            maker_fee_pct = (ld.maker_fee + sd.maker_fee) * 2        # total round trip %
            taker_fee_pct = (ld.taker_fee + sd.taker_fee) * 2        # total round trip %

            # Net APR = gross funding spread APR minus one-time fee cost.
            # The fee is a flat drag, not annualised — it's paid once regardless
            # of how long you hold. We subtract it as-is from the gross APR so
            # the resulting number tells the user "this is your APR after entry
            # and exit fees are accounted for over one full year of holding".
            net_apr_maker = funding_delta_apr - maker_fee_pct
            net_apr_taker = funding_delta_apr - taker_fee_pct

            # ── Break-even ────────────────────────────────────────────────────
            # Total one-way entry cost (open both legs once)
            one_way_entry_pct = entry_exit_fee_pct(ld.taker_fee, sd.taker_fee)
            total_round_trip_pct = one_way_entry_pct * 2  # entry + exit
            breakeven_hours = calculate_breakeven_hours(
                funding_delta_apr, total_round_trip_pct
            )

            long_leg = ArbitrageLeg(
                exchange=ld.exchange,
                token=ld.token,
                side="long",
                funding_rate=ld.funding_rate,
                funding_rate_8h=ld.funding_rate_8h,
                funding_apr=ld.funding_apr,
                mark_price=ld.mark_price,
                index_price=ld.index_price,
                open_interest_usd=ld.open_interest_usd,
                maker_fee=ld.maker_fee,
                taker_fee=ld.taker_fee,
                is_stale=long_snap.is_stale(),
            )
            short_leg = ArbitrageLeg(
                exchange=sd.exchange,
                token=sd.token,
                side="short",
                funding_rate=sd.funding_rate,
                funding_rate_8h=sd.funding_rate_8h,
                funding_apr=sd.funding_apr,
                mark_price=sd.mark_price,
                index_price=sd.index_price,
                open_interest_usd=sd.open_interest_usd,
                maker_fee=sd.maker_fee,
                taker_fee=sd.taker_fee,
                is_stale=short_snap.is_stale(),
            )

            return ArbitrageResult(
                token=ld.token,
                long_leg=long_leg,
                short_leg=short_leg,
                funding_delta_apr=round(funding_delta_apr, 6),
                price_spread_pct=round(price_spread_pct, 6),
                net_apr_maker=round(net_apr_maker, 6),
                net_apr_taker=round(net_apr_taker, 6),
                entry_fee_pct_taker=round(one_way_entry_pct, 6),
                breakeven_hours_taker=(
                    round(breakeven_hours, 2) if breakeven_hours else None
                ),
                detected_at=now_ms,
            )

        except (TypeError, ValueError, ZeroDivisionError) as exc:
            logger.warning("ArbitragePair computation error: %s", exc)
            return None

    # ── Redis publish ─────────────────────────────────────────────────────────

    async def calculate_and_publish(self) -> int:
        """Run calculation and publish results to Redis. Returns opportunity count."""
        results = self.calculate()
        payload = json.dumps([self._result_to_dict(r) for r in results])

        try:
            pipe = self._redis.pipeline(transaction=False)
            pipe.set("arbitrage:current", payload, ex=REDIS_KEY_TTL)
            pipe.publish("arbitrage:updates", payload)
            await pipe.execute()
            logger.debug("Published %d arbitrage opportunities.", len(results))
        except Exception as exc:
            logger.error("Failed to publish arbitrage results: %s", exc)

        return len(results)

    @staticmethod
    def _result_to_dict(r: ArbitrageResult) -> dict:
        d = asdict(r)
        return d
