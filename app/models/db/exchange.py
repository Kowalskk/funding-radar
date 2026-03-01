"""
app/models/db/exchange.py — Exchange SQLAlchemy ORM model.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Numeric, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.db.token import ExchangeToken


class Exchange(Base):
    """Represents a perpetual DEX (e.g. Hyperliquid, GMX, dYdX)."""

    __tablename__ = "exchanges"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Fees stored as decimal percentages, e.g. 0.01 = 0.01%
    maker_fee: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=6), nullable=False, default=Decimal("0")
    )
    taker_fee: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=6), nullable=False, default=Decimal("0")
    )

    # How often the exchange settles funding (in hours: 1, 4, 8, etc.)
    funding_interval_hours: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=8
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ── Relationships ─────────────────────────────────────────────────────────
    exchange_tokens: Mapped[list[ExchangeToken]] = relationship(
        "ExchangeToken",
        back_populates="exchange",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Exchange slug={self.slug!r} name={self.name!r}>"
