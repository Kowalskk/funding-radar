"""
app/models/db/funding_rate.py — FundingRate SQLAlchemy ORM model.

This table is converted to a TimescaleDB hypertable in the initial migration,
partitioned by the `time` column.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Numeric
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.db.exchange import Exchange
    from app.models.db.token import Token


class FundingRate(Base):
    """Stores a single funding rate snapshot for a token on an exchange.

    ``time`` is the primary dimension for the TimescaleDB hypertable.
    The composite primary key (time, exchange_id, token_id) ensures
    uniqueness while remaining compatible with TimescaleDB chunk requirements.
    """

    __tablename__ = "funding_rates"
    __table_args__ = (
        # Composite index for the most common query pattern
        Index(
            "ix_funding_rates_exchange_token_time",
            "exchange_id",
            "token_id",
            "time",
        ),
        # Useful for latest-rate-per-exchange queries
        Index("ix_funding_rates_time_desc", "time"),
        # Required by TimescaleDB: primary key must include the partitioning column
        {"postgresql_partition_by": None},  # handled by migration, not DDL here
    )

    # TimescaleDB requires `time` to be part of the primary key
    time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False
    )
    exchange_id: Mapped[int] = mapped_column(
        ForeignKey("exchanges.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    token_id: Mapped[int] = mapped_column(
        ForeignKey("tokens.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )

    # Core funding data
    # Raw rate per funding interval (e.g. 0.0001 = 0.01%)
    funding_rate: Mapped[Decimal] = mapped_column(
        Numeric(precision=20, scale=10), nullable=False
    )
    # Annualised to an 8h basis for cross-exchange comparison
    funding_rate_8h: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=20, scale=10), nullable=True
    )
    # Annualised percentage rate: funding_rate_8h * 3 * 365 * 100
    funding_apr: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=20, scale=6), nullable=True
    )

    # Market context
    mark_price: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=24, scale=8), nullable=True
    )
    index_price: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=24, scale=8), nullable=True
    )
    open_interest_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=28, scale=4), nullable=True
    )
    volume_24h_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=28, scale=4), nullable=True
    )
    # (mark_price - index_price) / index_price * 100
    price_spread_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=12, scale=6), nullable=True
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    exchange: Mapped[Exchange] = relationship("Exchange", lazy="joined")
    token: Mapped[Token] = relationship("Token", back_populates="funding_rates", lazy="joined")

    def __repr__(self) -> str:
        return (
            f"<FundingRate time={self.time!r} exchange_id={self.exchange_id} "
            f"token_id={self.token_id} rate={self.funding_rate}>"
        )
