"""Initial schema — all tables, TimescaleDB hypertable, indexes, seed data.

Revision ID: 0001
Revises:
Create Date: 2026-03-01

This migration:
  1. Enables the TimescaleDB extension
  2. Creates the `user_tier_enum` Postgres enum type
  3. Creates tables: exchanges, tokens, exchange_tokens, funding_rates, users,
     notification_rules
  4. Converts `funding_rates` to a TimescaleDB hypertable (partitioned by `time`)
  5. Creates all performance indexes (including a TimescaleDB segment-by index)
  6. Seeds Hyperliquid and Aster as the initial exchanges
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── helpers ────────────────────────────────────────────────────────────────────


def _execute(sql: str, *args) -> None:
    """Run raw SQL via op.execute (supports % formatting)."""
    op.execute(sa.text(sql))


# ── upgrade ────────────────────────────────────────────────────────────────────


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. TimescaleDB extension
    # ------------------------------------------------------------------
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;"))

    # ------------------------------------------------------------------
    # 2. Enum type
    # ------------------------------------------------------------------
    user_tier_enum = postgresql.ENUM(
        "free", "pro", "custom", name="user_tier_enum", create_type=False
    )
    user_tier_enum.create(op.get_bind(), checkfirst=True)

    # ------------------------------------------------------------------
    # 3. exchanges
    # ------------------------------------------------------------------
    op.create_table(
        "exchanges",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("logo_url", sa.Text(), nullable=True),
        sa.Column(
            "maker_fee", sa.Numeric(precision=10, scale=6), nullable=False, server_default="0"
        ),
        sa.Column(
            "taker_fee", sa.Numeric(precision=10, scale=6), nullable=False, server_default="0"
        ),
        sa.Column(
            "funding_interval_hours", sa.SmallInteger(), nullable=False, server_default="8"
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.create_index("ix_exchanges_slug", "exchanges", ["slug"], unique=True)

    # ------------------------------------------------------------------
    # 4. tokens
    # ------------------------------------------------------------------
    op.create_table(
        "tokens",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.create_index("ix_tokens_symbol", "tokens", ["symbol"], unique=True)

    # ------------------------------------------------------------------
    # 5. exchange_tokens
    # ------------------------------------------------------------------
    op.create_table(
        "exchange_tokens",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "exchange_id",
            sa.Integer(),
            sa.ForeignKey("exchanges.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "token_id",
            sa.Integer(),
            sa.ForeignKey("tokens.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("exchange_symbol", sa.String(64), nullable=False),
        sa.Column("max_leverage", sa.Numeric(precision=8, scale=2), nullable=True),
        sa.UniqueConstraint("exchange_id", "token_id", name="uq_exchange_token"),
    )
    op.create_index("ix_exchange_tokens_exchange_id", "exchange_tokens", ["exchange_id"])
    op.create_index("ix_exchange_tokens_token_id", "exchange_tokens", ["token_id"])

    # ------------------------------------------------------------------
    # 6. funding_rates  (regular table first, then hypertable)
    # ------------------------------------------------------------------
    op.create_table(
        "funding_rates",
        # Composite PK required by TimescaleDB
        sa.Column("time", sa.DateTime(timezone=True), nullable=False, primary_key=True),
        sa.Column(
            "exchange_id",
            sa.Integer(),
            sa.ForeignKey("exchanges.id", ondelete="CASCADE"),
            nullable=False,
            primary_key=True,
        ),
        sa.Column(
            "token_id",
            sa.Integer(),
            sa.ForeignKey("tokens.id", ondelete="CASCADE"),
            nullable=False,
            primary_key=True,
        ),
        # Core funding fields
        sa.Column("funding_rate", sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column("funding_rate_8h", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("funding_apr", sa.Numeric(precision=20, scale=6), nullable=True),
        # Market context
        sa.Column("mark_price", sa.Numeric(precision=24, scale=8), nullable=True),
        sa.Column("index_price", sa.Numeric(precision=24, scale=8), nullable=True),
        sa.Column("open_interest_usd", sa.Numeric(precision=28, scale=4), nullable=True),
        sa.Column("volume_24h_usd", sa.Numeric(precision=28, scale=4), nullable=True),
        sa.Column("price_spread_pct", sa.Numeric(precision=12, scale=6), nullable=True),
    )

    # Convert to TimescaleDB hypertable, partitioned by `time`, 1 day per chunk
    op.execute(
        sa.text(
            "SELECT create_hypertable("
            "  'funding_rates',"
            "  'time',"
            "  chunk_time_interval => INTERVAL '1 day',"
            "  if_not_exists => TRUE"
            ");"
        )
    )

    # Set compression policy: compress chunks older than 7 days
    op.execute(
        sa.text(
            "ALTER TABLE funding_rates SET ("
            "  timescaledb.compress,"
            "  timescaledb.compress_segmentby = 'exchange_id, token_id',"
            "  timescaledb.compress_orderby = 'time DESC'"
            ");"
        )
    )
    op.execute(
        sa.text(
            "SELECT add_compression_policy('funding_rates', INTERVAL '7 days', if_not_exists => TRUE);"
        )
    )

    # Performance indexes
    op.create_index(
        "ix_funding_rates_exchange_token_time",
        "funding_rates",
        ["exchange_id", "token_id", "time"],
    )
    op.create_index(
        "ix_funding_rates_token_time",
        "funding_rates",
        ["token_id", "time"],
    )
    op.create_index(
        "ix_funding_rates_funding_apr",
        "funding_rates",
        ["funding_apr"],
    )

    # ------------------------------------------------------------------
    # 7. users
    # ------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("hashed_password", sa.Text(), nullable=True),
        sa.Column("telegram_chat_id", sa.String(64), nullable=True),
        sa.Column(
            "tier",
            postgresql.ENUM("free", "pro", "custom", name="user_tier_enum", create_type=False),
            nullable=False,
            server_default="free",
        ),
        sa.Column("api_key", sa.String(64), nullable=False),
        sa.Column("stripe_customer_id", sa.String(128), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_api_key", "users", ["api_key"], unique=True)
    op.create_index("ix_users_stripe_customer_id", "users", ["stripe_customer_id"], unique=True)

    # Trigger to auto-update `updated_at` on every row change
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION update_updated_at_column()
            RETURNS TRIGGER AS $$
            BEGIN
                NEW.updated_at = NOW();
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_users_updated_at
                BEFORE UPDATE ON users
                FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
            """
        )
    )

    # ------------------------------------------------------------------
    # 8. notification_rules
    # ------------------------------------------------------------------
    op.create_table(
        "notification_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_symbol", sa.String(32), nullable=False),
        sa.Column("min_apr", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column(
            "exchanges",
            postgresql.ARRAY(sa.String(64)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index("ix_notification_rules_user_id", "notification_rules", ["user_id"])
    op.create_index(
        "ix_notification_rules_token_symbol", "notification_rules", ["token_symbol"]
    )

    # ------------------------------------------------------------------
    # 9. Seed data — initial exchanges
    # ------------------------------------------------------------------
    op.execute(
        sa.text(
            """
            INSERT INTO exchanges (slug, name, logo_url, maker_fee, taker_fee, funding_interval_hours, is_active)
            VALUES
                (
                    'hyperliquid',
                    'Hyperliquid',
                    'https://hyperliquid.xyz/favicon.ico',
                    0.01,       -- maker: 0.01%
                    0.035,      -- taker: 0.035%
                    1,          -- funding every 1 hour
                    true
                ),
                (
                    'aster',
                    'Aster',
                    NULL,
                    0.01,       -- maker: 0.01%
                    0.035,      -- taker: 0.035%
                    8,          -- funding every 8 hours
                    true
                )
            ON CONFLICT (slug) DO NOTHING;
            """
        )
    )


# ── downgrade ──────────────────────────────────────────────────────────────────


def downgrade() -> None:
    # Remove tables in reverse dependency order
    op.drop_table("notification_rules")
    op.drop_table("users")
    op.drop_table("funding_rates")
    op.drop_table("exchange_tokens")
    op.drop_table("tokens")
    op.drop_table("exchanges")

    # Drop enum type
    op.execute(sa.text("DROP TYPE IF EXISTS user_tier_enum;"))

    # Drop the updated_at trigger function
    op.execute(sa.text("DROP FUNCTION IF EXISTS update_updated_at_column() CASCADE;"))

    # Note: TimescaleDB extension is NOT dropped here on purpose;
    # removing an extension is irreversible and destructive.
