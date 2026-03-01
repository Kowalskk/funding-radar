"""
app/models/db/token.py — Token and ExchangeToken SQLAlchemy ORM models.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.db.exchange import Exchange
    from app.models.db.funding_rate import FundingRate


class Token(Base):
    """A tradeable asset (e.g. BTC, ETH, SOL)."""

    __tablename__ = "tokens"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ── Relationships ─────────────────────────────────────────────────────────
    exchange_tokens: Mapped[list[ExchangeToken]] = relationship(
        "ExchangeToken",
        back_populates="token",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    funding_rates: Mapped[list[FundingRate]] = relationship(
        "FundingRate",
        back_populates="token",
        lazy="dynamic",
    )

    def __repr__(self) -> str:
        return f"<Token symbol={self.symbol!r}>"


class ExchangeToken(Base):
    """Junction table mapping an Exchange to a Token it lists.

    Stores the exchange-specific ticker symbol and max leverage.
    """

    __tablename__ = "exchange_tokens"
    __table_args__ = (
        UniqueConstraint("exchange_id", "token_id", name="uq_exchange_token"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    exchange_id: Mapped[int] = mapped_column(
        ForeignKey("exchanges.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_id: Mapped[int] = mapped_column(
        ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # The symbol used by this specific exchange (may differ from the canonical one)
    exchange_symbol: Mapped[str] = mapped_column(String(64), nullable=False)

    max_leverage: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=8, scale=2), nullable=True
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    exchange: Mapped[Exchange] = relationship("Exchange", back_populates="exchange_tokens")
    token: Mapped[Token] = relationship("Token", back_populates="exchange_tokens")

    def __repr__(self) -> str:
        return (
            f"<ExchangeToken exchange_id={self.exchange_id} "
            f"token_id={self.token_id} symbol={self.exchange_symbol!r}>"
        )
