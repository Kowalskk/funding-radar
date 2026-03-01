"""app/models/schemas — Pydantic schemas for the funding-radar API."""

from app.models.schemas.arbitrage import (
    ArbitrageLeg,
    ArbitrageListResponse,
    ArbitrageOpportunity,
)
from app.models.schemas.funding import (
    ExchangeInfo,
    FundingHistoryPoint,
    FundingHistoryResponse,
    FundingRateResponse,
    FundingRateSnapshot,
    MarketOverviewResponse,
    MarketOverviewRow,
    TokenInfo,
)
from app.models.schemas.simulator import (
    SimulatorLegConfig,
    SimulatorLegResult,
    SimulatorRequest,
    SimulatorResponse,
)

__all__ = [
    # Funding
    "ExchangeInfo",
    "TokenInfo",
    "FundingRateResponse",
    "FundingRateSnapshot",
    "FundingHistoryPoint",
    "FundingHistoryResponse",
    "MarketOverviewRow",
    "MarketOverviewResponse",
    # Arbitrage
    "ArbitrageLeg",
    "ArbitrageOpportunity",
    "ArbitrageListResponse",
    # Simulator
    "SimulatorLegConfig",
    "SimulatorRequest",
    "SimulatorLegResult",
    "SimulatorResponse",
]
