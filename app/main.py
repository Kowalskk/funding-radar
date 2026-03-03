"""
app/main.py — FastAPI application entry point.

Manages the full lifecycle:
  • PostgreSQL async pool (SQLAlchemy + asyncpg)
  • Redis async connection pool
  • APScheduler for periodic data-collection jobs
  • API v1 router registration
  • WebSocket endpoint for real-time funding rate streaming
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from app.config import Settings, get_settings
from app.core.database import close_db, init_db
from app.core.redis import close_redis, get_redis, init_redis
from app.core.scheduler import (
    add_interval_job,
    init_scheduler,
    shutdown_scheduler,
)
from app.collectors import AsterCollector, CollectorConfig, CollectorRegistry, HyperliquidCollector, ExtendedCollector, PacificaCollector
from app.services import FundingService
from app.services.notification_service import NotificationService, TelegramSender
from app.bot.telegram_bot import TelegramBotRunner
from app.core.websocket_manager import WebSocketManager
from app.core.redis_ws_bridge import RedisBridge

# ── Logging setup ─────────────────────────────────────────────────────────────


def _configure_logging(settings: Settings) -> None:
    """Configure stdlib and structlog based on settings."""
    level = getattr(logging, settings.log_level, logging.INFO)

    if settings.log_format == "json":
        processors = [
            structlog.contextvars.merge_contextvars,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors = [
            structlog.contextvars.merge_contextvars,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
            structlog.stdlib.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ]

    structlog.configure(
        processors=processors,  # type: ignore[arg-type]
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )


logger = structlog.get_logger(__name__)


# ── Collector registry (module-level singleton) ───────────────────────────────

_collector_registry: CollectorRegistry | None = None


def get_collector_registry() -> CollectorRegistry:
    if _collector_registry is None:
        raise RuntimeError("CollectorRegistry not initialised.")
    return _collector_registry


_funding_service: FundingService | None = None


def get_funding_service() -> FundingService:
    if _funding_service is None:
        raise RuntimeError("FundingService not initialised.")
    return _funding_service


_ws_manager: WebSocketManager | None = None
_redis_bridge: RedisBridge | None = None


def get_ws_manager() -> WebSocketManager:
    if _ws_manager is None:
        raise RuntimeError("WebSocketManager not initialised.")
    return _ws_manager


_notification_service: NotificationService | None = None
_telegram_bot: TelegramBotRunner | None = None


def get_notification_service() -> NotificationService:
    if _notification_service is None:
        raise RuntimeError("NotificationService not initialised.")
    return _notification_service


async def cleanup_expired_cache() -> None:
    """Remove stale cache keys (Redis TTL handles most cases automatically)."""
    log = structlog.get_logger("jobs.cache_cleanup")
    log.debug("Running cache cleanup…")


# ── Lifespan ──────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application startup and shutdown lifecycle."""
    settings: Settings = get_settings()
    _configure_logging(settings)

    logger.info(
        "Starting %s [%s]",
        settings.app_name,
        settings.app_env,
    )

    # ── Startup ──────────────────────────────────────────
    global _collector_registry
    try:
        await init_db(settings)
        await init_redis(settings)

        _scheduler = init_scheduler(settings)

        # Periodic scheduler jobs
        add_interval_job(
            cleanup_expired_cache,
            minutes=settings.scheduler_cache_cleanup_interval_minutes,
            job_id="cleanup_expired_cache",
        )

        # ── Collector registry ────────────────────────────
        collector_config = CollectorConfig(
            rest_poll_interval=settings.scheduler_funding_rate_interval_seconds,
        )
        _collector_registry = CollectorRegistry(
            redis_client=get_redis(),
            config=collector_config,
        )
        _collector_registry.register("hyperliquid", HyperliquidCollector)
        _collector_registry.register("aster", AsterCollector)
        _collector_registry.register("extended", ExtendedCollector)
        _collector_registry.register("pacifica", PacificaCollector)
        await _collector_registry.start_all()

        # ── Funding service ───────────────────────────────
        global _funding_service
        _funding_service = FundingService(
            redis=get_redis(),
            recalculate_every_seconds=settings.scheduler_funding_rate_interval_seconds,
        )
        await _funding_service.start()

        # ── WebSocket manager + Redis bridge ───────────────
        global _ws_manager, _redis_bridge
        _ws_manager = WebSocketManager()
        await _ws_manager.start()
        _redis_bridge = RedisBridge(redis=get_redis(), manager=_ws_manager)
        await _redis_bridge.start()

        # ── Notification service + Telegram bot ─────────────
        global _notification_service, _telegram_bot
        _telegram_sender = TelegramSender(
            bot_token=settings.telegram_bot_token.get_secret_value()
            if settings.telegram_bot_token else ""
        )
        await _telegram_sender.start()
        _notification_service = NotificationService(sender=_telegram_sender)

        # Scheduler job: check alert rules every 30 seconds
        add_interval_job(
            _notification_service.check_and_notify,
            seconds=30,
            job_id="notification_check",
        )

        # Telegram bot polling (only if token is configured)
        _telegram_bot = TelegramBotRunner(
            bot_token=settings.telegram_bot_token.get_secret_value()
            if settings.telegram_bot_token else ""
        )
        await _telegram_bot.start()

        logger.info("All services initialised — application ready.")
        yield  # ← application serves requests here

    except Exception as exc:
        logger.error("Startup failed: %s", exc, exc_info=True)
        raise

    # ── Shutdown ─────────────────────────────────────────
    finally:
        logger.info("Shutting down…")
        if _telegram_bot is not None:
            await _telegram_bot.stop()
        if _redis_bridge is not None:
            await _redis_bridge.stop()
        if _ws_manager is not None:
            await _ws_manager.stop()
        if _funding_service is not None:
            await _funding_service.stop()
        if _collector_registry is not None:
            await _collector_registry.stop_all()
        shutdown_scheduler()
        await close_redis()
        await close_db()
        logger.info("Graceful shutdown complete.")


# ── Application factory ───────────────────────────────────────────────────────


def create_app() -> FastAPI:
    settings = get_settings()

    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="Real-time perpetual DEX funding rate aggregator.",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
    )

    # ── CORS ─────────────────────────────────────────────
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Global exception handler (dev) ─────────────────────────
    if settings.app_debug:
        import traceback as _tb

        from fastapi.responses import JSONResponse

        @application.exception_handler(Exception)
        async def _debug_exception_handler(request: Request, exc: Exception) -> JSONResponse:
            tb = _tb.format_exception(type(exc), exc, exc.__traceback__)
            logging.getLogger("uvicorn.error").error("Unhandled: %s\n%s", exc, "".join(tb))
            return JSONResponse(
                status_code=500,
                content={"detail": str(exc), "traceback": tb},
            )

    # ── API v1 router ──────────────────────────────────────
    from app.api.v1.router import api_v1_router
    application.include_router(api_v1_router)

    # ── WebSocket router ─────────────────────────────────
    from app.api.ws.funding_stream import router as ws_router
    application.include_router(ws_router)

    # ── Health check ─────────────────────────────────────
    @application.get("/health", tags=["Monitoring"])
    async def health() -> dict:
        """Liveness probe — always returns 200 when the process is up."""
        return {"status": "ok", "service": settings.app_name}

    @application.get("/ready", tags=["Monitoring"])
    async def readiness() -> dict:
        """Readiness probe — verifies DB and Redis are reachable."""
        redis = get_redis()
        await redis.ping()
        return {"status": "ready"}

    # ── Collector status ──────────────────────────────────
    @application.get("/collectors/status", tags=["Monitoring"])
    async def collectors_status() -> dict:
        """Return the running state of all registered collectors."""
        return get_collector_registry().status()

    @application.get("/service/status", tags=["Monitoring"])
    async def service_status() -> dict:
        """Return FundingService diagnostics: update counts, normalizer stats, recalc lag."""
        return get_funding_service().status

    @application.get("/ws/status", tags=["Monitoring"])
    async def ws_status() -> dict:
        """Return WebSocket manager stats: connection count, active channels."""
        return get_ws_manager().stats

    return application


# ── ASGI entry point ──────────────────────────────────────────────────────────

app = create_app()
