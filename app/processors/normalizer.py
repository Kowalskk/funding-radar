"""
app/processors/normalizer.py — DataNormalizer: in-memory cross-exchange state.

Consumes `NormalizedFundingData` updates and maintains:
  • The latest snapshot per (exchange, token)
  • A grouped view per token with data from all exchanges
  • Staleness tracking — marks data stale after `stale_after_seconds`

Thread-safety note: this module is designed for single-threaded async use
(single asyncio event loop). No locks needed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterator

from app.collectors.base import NormalizedFundingData

# How long before an exchange snapshot is considered stale (seconds)
DEFAULT_STALE_AFTER = 120


@dataclass
class ExchangeSnapshot:
    """Latest funding data for one (exchange, token) pair with staleness info."""

    data: NormalizedFundingData
    received_at: float = field(default_factory=time.monotonic)

    def is_stale(self, stale_after: float = DEFAULT_STALE_AFTER) -> bool:
        return (time.monotonic() - self.received_at) > stale_after

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.received_at


@dataclass
class TokenView:
    """Aggregated cross-exchange view for a single token."""

    token: str
    snapshots: dict[str, ExchangeSnapshot] = field(default_factory=dict)

    def live_snapshots(self, stale_after: float = DEFAULT_STALE_AFTER) -> list[ExchangeSnapshot]:
        """Return only non-stale snapshots, sorted by funding_apr descending."""
        live = [s for s in self.snapshots.values() if not s.is_stale(stale_after)]
        return sorted(live, key=lambda s: s.data.funding_apr, reverse=True)

    def all_snapshots(self) -> list[ExchangeSnapshot]:
        return sorted(
            self.snapshots.values(),
            key=lambda s: s.data.funding_apr,
            reverse=True,
        )

    def max_apr(self, stale_after: float = DEFAULT_STALE_AFTER) -> float:
        live = self.live_snapshots(stale_after)
        return max((s.data.funding_apr for s in live), default=0.0)

    def min_apr(self, stale_after: float = DEFAULT_STALE_AFTER) -> float:
        live = self.live_snapshots(stale_after)
        return min((s.data.funding_apr for s in live), default=0.0)

    def spread_apr(self, stale_after: float = DEFAULT_STALE_AFTER) -> float:
        """Max APR minus Min APR — the gross arbitrage spread available."""
        live = self.live_snapshots(stale_after)
        if len(live) < 2:
            return 0.0
        return live[0].data.funding_apr - live[-1].data.funding_apr

    def exchange_count(self, stale_after: float = DEFAULT_STALE_AFTER) -> int:
        return len(self.live_snapshots(stale_after))


class DataNormalizer:
    """In-memory store of cross-exchange funding rate state.

    Key methods:
        update(data)         — ingest a NormalizedFundingData; returns True if changed
        get_token_view(token) — TokenView for one token
        iter_tokens()        — iterate all TokenView objects
        arbitrage_candidates() — tokens with 2+ live exchanges, sorted by spread
    """

    def __init__(self, stale_after_seconds: float = DEFAULT_STALE_AFTER) -> None:
        self._stale_after = stale_after_seconds
        # (exchange, token) → ExchangeSnapshot
        self._snapshots: dict[tuple[str, str], ExchangeSnapshot] = {}
        # token → TokenView
        self._token_views: dict[str, TokenView] = {}
        self._update_count: int = 0
        self._last_update_at: float = 0.0

    # ── Ingestion ─────────────────────────────────────────────────────────────

    def update(self, data: NormalizedFundingData) -> bool:
        """Store the latest snapshot. Returns True (always updates in-place)."""
        key = (data.exchange, data.token)
        snapshot = ExchangeSnapshot(data=data)

        self._snapshots[key] = snapshot

        # Update or create the token view
        if data.token not in self._token_views:
            self._token_views[data.token] = TokenView(token=data.token)
        self._token_views[data.token].snapshots[data.exchange] = snapshot

        self._update_count += 1
        self._last_update_at = time.monotonic()
        return True

    def update_batch(self, items: list[NormalizedFundingData]) -> int:
        """Ingest a list of snapshots; returns how many were stored."""
        count = 0
        for item in items:
            if self.update(item):
                count += 1
        return count

    # ── Query ─────────────────────────────────────────────────────────────────

    def get_snapshot(
        self, exchange: str, token: str
    ) -> ExchangeSnapshot | None:
        return self._snapshots.get((exchange, token))

    def get_token_view(self, token: str) -> TokenView | None:
        return self._token_views.get(token)

    def iter_tokens(self) -> Iterator[TokenView]:
        return iter(self._token_views.values())

    def all_tokens(self) -> list[str]:
        return sorted(self._token_views.keys())

    def arbitrage_candidates(self) -> list[TokenView]:
        """Return TokenViews with 2+ live exchanges, sorted by spread_apr desc."""
        candidates = [
            tv
            for tv in self._token_views.values()
            if tv.exchange_count(self._stale_after) >= 2
        ]
        return sorted(
            candidates,
            key=lambda tv: tv.spread_apr(self._stale_after),
            reverse=True,
        )

    def get_latest(self, exchange: str, token: str) -> NormalizedFundingData | None:
        snap = self._snapshots.get((exchange, token))
        return snap.data if snap else None

    def get_all_latest(self) -> list[NormalizedFundingData]:
        """Return the latest snapshot for every (exchange, token) pair, freshest first."""
        return sorted(
            (s.data for s in self._snapshots.values()),
            key=lambda d: d.timestamp,
            reverse=True,
        )

    # ── Housekeeping ──────────────────────────────────────────────────────────

    def purge_stale(self) -> int:
        """Remove snapshots older than `stale_after_seconds`. Returns purge count."""
        stale_keys = [
            k for k, s in self._snapshots.items() if s.is_stale(self._stale_after)
        ]
        for key in stale_keys:
            exchange, token = key
            del self._snapshots[key]
            tv = self._token_views.get(token)
            if tv:
                tv.snapshots.pop(exchange, None)
                if not tv.snapshots:
                    del self._token_views[token]
        return len(stale_keys)

    # ── Diagnostics ───────────────────────────────────────────────────────────

    @property
    def stats(self) -> dict:
        return {
            "total_snapshots": len(self._snapshots),
            "total_tokens": len(self._token_views),
            "update_count": self._update_count,
            "last_update_ago_s": round(time.monotonic() - self._last_update_at, 1),
            "arbitrage_candidates": len(self.arbitrage_candidates()),
        }
