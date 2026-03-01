"""
app/core/scheduler.py — APScheduler async job scheduler setup.
"""

from __future__ import annotations

import logging
from typing import Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.executors.asyncio import AsyncIOExecutor

from app.config import Settings

logger = logging.getLogger(__name__)

# ── Module-level singleton ────────────────────────────────────────────────────

_scheduler: AsyncIOScheduler | None = None


# ── Lifecycle ─────────────────────────────────────────────────────────────────


def init_scheduler(settings: Settings) -> AsyncIOScheduler:
    """Create and start the APScheduler instance.

    Job stores and executors are kept in-memory for simplicity.
    Swap to SQLAlchemyJobStore for persistence across restarts.
    """
    global _scheduler

    logger.info("Initialising job scheduler…")

    jobstores = {
        "default": MemoryJobStore(),
    }
    executors = {
        "default": AsyncIOExecutor(),
    }
    job_defaults = {
        "coalesce": True,          # merge missed executions into one
        "max_instances": 1,        # prevent overlapping runs
        "misfire_grace_time": 30,  # allow 30s late start
    }

    _scheduler = AsyncIOScheduler(
        jobstores=jobstores,
        executors=executors,
        job_defaults=job_defaults,
        timezone="UTC",
    )

    _scheduler.start()
    logger.info("Job scheduler started.")
    return _scheduler


def shutdown_scheduler() -> None:
    """Gracefully shut down the scheduler, waiting for running jobs."""
    global _scheduler

    if _scheduler is not None and _scheduler.running:
        logger.info("Shutting down job scheduler…")
        _scheduler.shutdown(wait=True)
        _scheduler = None
        logger.info("Job scheduler stopped.")


def get_scheduler() -> AsyncIOScheduler:
    """Return the running scheduler or raise if not initialised."""
    if _scheduler is None or not _scheduler.running:
        raise RuntimeError("Scheduler not initialised. Call init_scheduler() first.")
    return _scheduler


# ── Job registration helpers ──────────────────────────────────────────────────


def add_interval_job(
    func: Callable,
    *,
    seconds: int | None = None,
    minutes: int | None = None,
    job_id: str | None = None,
    **kwargs,
) -> str:
    """Register a recurring interval job on the global scheduler.

    Returns the job ID.
    """
    scheduler = get_scheduler()
    job = scheduler.add_job(
        func,
        trigger="interval",
        seconds=seconds or 0,
        minutes=minutes or 0,
        id=job_id or func.__name__,
        replace_existing=True,
        **kwargs,
    )
    logger.info(
        "Registered interval job '%s' (every %ss / %sm)",
        job.id,
        seconds,
        minutes,
    )
    return job.id


def add_cron_job(
    func: Callable,
    *,
    cron_expression: str,
    job_id: str | None = None,
    **kwargs,
) -> str:
    """Register a cron-style job on the global scheduler.

    cron_expression is a standard 5-field cron string, e.g. '*/5 * * * *'.
    Returns the job ID.
    """
    scheduler = get_scheduler()
    minute, hour, day, month, day_of_week = cron_expression.split()
    job = scheduler.add_job(
        func,
        trigger="cron",
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
        id=job_id or func.__name__,
        replace_existing=True,
        **kwargs,
    )
    logger.info("Registered cron job '%s' (%s)", job.id, cron_expression)
    return job.id
