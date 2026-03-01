"""
app/core/database.py — SQLAlchemy async engine and session factory.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.config import Settings

logger = logging.getLogger(__name__)

# ── ORM base class ────────────────────────────────────────────────────────────


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


# ── Module-level singletons ───────────────────────────────────────────────────

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


# ── Lifecycle ─────────────────────────────────────────────────────────────────


async def init_db(settings: Settings) -> None:
    """Create the async engine and session factory.

    Uses NullPool when running tests to avoid pool-related issues with
    pytest-asyncio's event loop management.
    """
    global _engine, _session_factory

    logger.info("Initialising database connection pool…")

    _engine = create_async_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout,
        pool_pre_ping=True,          # detect stale connections
        pool_recycle=3600,           # recycle connections every hour
        echo=settings.app_debug,     # log SQL in debug mode
        future=True,
    )

    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,      # prevents lazy-load errors after commit
        autoflush=False,
        autocommit=False,
    )

    logger.info("Database pool initialised (pool_size=%d)", settings.database_pool_size)


async def close_db() -> None:
    """Dispose the engine and release all pool connections."""
    global _engine, _session_factory

    if _engine is not None:
        logger.info("Closing database connection pool…")
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("Database pool closed.")


# ── Session dependency ────────────────────────────────────────────────────────


@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager that yields a database session.

    Rolls back on exception and always closes the session.
    """
    if _session_factory is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")

    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session."""
    async with get_db_session() as session:
        yield session


def get_engine() -> AsyncEngine:
    """Return the current engine or raise if not initialised."""
    if _engine is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    return _engine
