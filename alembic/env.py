"""
alembic/env.py — Async Alembic environment for SQLAlchemy 2.0 + asyncpg.

Uses the `run_sync` pattern so Alembic's synchronous API works over an
async connection (required when using asyncpg driver).
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# ── Load all models so their metadata is registered ──────────────────────────
# This import must happen before `target_metadata` is assigned.
import app.models.db  # noqa: F401 — side-effect: registers all ORM tables
from app.core.database import Base
from app.config import get_settings

# Alembic Config object — provides access to .ini values
config = context.config

# Set up Python logging from alembic.ini [loggers] sections (if present)
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata for autogenerate support
target_metadata = Base.metadata

# Override sqlalchemy.url from application settings at runtime
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)


# ── Offline mode (generate SQL script without a live connection) ──────────────


def run_migrations_offline() -> None:
    """Emit migration SQL without connecting to the database.

    Useful for generating SQL scripts to review or apply manually.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Include PostgreSQL-specific constructs
        include_schemas=True,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


# ── Online mode (connect and apply migrations) ─────────────────────────────────


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations via run_sync."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # no pool needed for migration runs
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online (connected) migration mode."""
    asyncio.run(run_async_migrations())


# ── Dispatch ──────────────────────────────────────────────────────────────────

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
