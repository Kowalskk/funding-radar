"""
app/models/db/user.py — User and NotificationRule SQLAlchemy ORM models.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

import enum


class UserTier(str, enum.Enum):
    FREE = "free"
    PRO = "pro"
    CUSTOM = "custom"


class User(Base):
    """Application user (API consumer / alert subscriber)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)

    # Hashed password (passlib bcrypt)
    hashed_password: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Optional Telegram integration
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Subscription tier
    tier: Mapped[UserTier] = mapped_column(
        Enum(UserTier, name="user_tier_enum", create_type=True,
             values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=UserTier.FREE,
        server_default=UserTier.FREE.value,
    )

    # REST API key — generated on user creation
    api_key: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
        default=lambda: secrets.token_urlsafe(32),
    )

    # Stripe
    stripe_customer_id: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    notification_rules: Mapped[list[NotificationRule]] = relationship(
        "NotificationRule",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r} tier={self.tier.value}>"


class NotificationRule(Base):
    """An alert rule that triggers when a token's APR crosses a threshold.

    ``exchanges`` is stored as a PostgreSQL text array (empty = all exchanges).
    """

    __tablename__ = "notification_rules"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Canonical token symbol (e.g. "BTC", "ETH")
    token_symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # Minimum annualised rate (in %) to trigger the alert
    min_apr: Mapped[float] = mapped_column(Numeric(precision=12, scale=4), nullable=False)

    # PostgreSQL array of exchange slugs; empty = any exchange
    exchanges: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, default=list, server_default="{}"
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    user: Mapped[User] = relationship("User", back_populates="notification_rules")

    def __repr__(self) -> str:
        return (
            f"<NotificationRule id={self.id} user_id={self.user_id} "
            f"token={self.token_symbol!r} min_apr={self.min_apr}>"
        )
