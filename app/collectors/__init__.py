"""app/collectors — Exchange funding rate collectors."""

from app.collectors.base import BaseCollector, CollectorConfig, NormalizedFundingData
from app.collectors.aster import AsterCollector
from app.collectors.hyperliquid import HyperliquidCollector
from app.collectors.registry import CollectorRegistry

__all__ = [
    "NormalizedFundingData",
    "CollectorConfig",
    "BaseCollector",
    "AsterCollector",
    "HyperliquidCollector",
    "CollectorRegistry",
]
