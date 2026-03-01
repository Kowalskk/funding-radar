"""
app/collectors/registry.py — Dynamic collector registry and lifecycle manager.

Usage:
    registry = CollectorRegistry(redis_client)
    registry.register("hyperliquid", HyperliquidCollector)
    await registry.start_all()
    ...
    await registry.stop_all()

The registry wraps each collector in its own asyncio.Task so that a crash
in one collector does not bring down the others. A background watchdog
task monitors for unexpected task exits and restarts them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Type

from redis.asyncio import Redis

from app.collectors.base import BaseCollector, CollectorConfig

logger = logging.getLogger(__name__)


class CollectorRegistry:
    """Manages registering, starting, and stopping all exchange collectors."""

    def __init__(
        self,
        redis_client: Redis,
        config: CollectorConfig | None = None,
    ) -> None:
        self._redis = redis_client
        self._config = config or CollectorConfig()
        self._registry: dict[str, Type[BaseCollector]] = {}
        self._instances: dict[str, BaseCollector] = {}
        self._watchdog_task: asyncio.Task | None = None
        self._running = False

    # ── Registration ──────────────────────────────────────────────────────────

    def register(
        self,
        name: str,
        collector_class: Type[BaseCollector],
        config: CollectorConfig | None = None,
    ) -> None:
        """Register a collector class under the given name.

        If `config` is given it overrides the registry-level default for this
        specific collector only.
        """
        if name in self._registry:
            logger.warning("Overwriting already registered collector '%s'.", name)
        self._registry[name] = collector_class
        # Stash a per-collector config override under a private attribute
        collector_class._registry_config = config  # type: ignore[attr-defined]
        logger.info("Registered collector '%s' → %s.", name, collector_class.__name__)

    def registered_names(self) -> list[str]:
        return list(self._registry.keys())

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start_all(self) -> None:
        """Instantiate and start every registered collector."""
        if self._running:
            logger.warning("Registry already running.")
            return

        self._running = True
        logger.info(
            "Starting %d collector(s): %s",
            len(self._registry),
            list(self._registry.keys()),
        )

        for name, cls in self._registry.items():
            per_collector_config = getattr(cls, "_registry_config", None) or self._config
            try:
                instance = cls(
                    redis_client=self._redis,
                    config=per_collector_config,
                )
                self._instances[name] = instance
                await instance.start()
                logger.info("Collector '%s' started.", name)
            except Exception as exc:
                logger.error(
                    "Failed to start collector '%s': %s", name, exc, exc_info=True
                )

        # Start watchdog
        self._watchdog_task = asyncio.create_task(
            self._watchdog(), name="collector_registry_watchdog"
        )

    async def stop_all(self) -> None:
        """Gracefully stop all running collectors."""
        if not self._running:
            return

        logger.info("Stopping all collectors…")
        self._running = False

        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass

        stop_tasks = [
            asyncio.create_task(inst.stop(), name=f"stop:{name}")
            for name, inst in self._instances.items()
        ]
        results = await asyncio.gather(*stop_tasks, return_exceptions=True)
        for name, result in zip(self._instances.keys(), results):
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                logger.error("Error stopping collector '%s': %s", name, result)

        self._instances.clear()
        logger.info("All collectors stopped.")

    async def restart(self, name: str) -> None:
        """Stop and restart a single collector by name."""
        if name not in self._instances:
            logger.warning("Cannot restart unknown collector '%s'.", name)
            return

        logger.info("Restarting collector '%s'…", name)
        instance = self._instances.pop(name)
        await instance.stop()

        cls = self._registry[name]
        per_collector_config = getattr(cls, "_registry_config", None) or self._config
        new_instance = cls(redis_client=self._redis, config=per_collector_config)
        self._instances[name] = new_instance
        await new_instance.start()
        logger.info("Collector '%s' restarted.", name)

    # ── Watchdog ──────────────────────────────────────────────────────────────

    async def _watchdog(self) -> None:
        """Periodically check for crashed collectors and restart them."""
        CHECK_INTERVAL = 30  # seconds

        while self._running:
            await asyncio.sleep(CHECK_INTERVAL)
            for name, instance in list(self._instances.items()):
                # A collector is considered crashed if none of its tasks are alive
                all_done = all(
                    t.done() for t in getattr(instance, "_tasks", [])
                )
                has_tasks = bool(getattr(instance, "_tasks", []))

                if has_tasks and all_done and instance._running:
                    logger.warning(
                        "Collector '%s' appears to have crashed. Restarting…", name
                    )
                    try:
                        await self.restart(name)
                    except Exception as exc:
                        logger.error(
                            "Watchdog failed to restart '%s': %s", name, exc
                        )

    # ── Status ────────────────────────────────────────────────────────────────

    def status(self) -> dict[str, dict]:
        """Return a summary of each collector's state for health endpoints."""
        result = {}
        for name, instance in self._instances.items():
            tasks = getattr(instance, "_tasks", [])
            result[name] = {
                "running": instance._running,
                "tasks": len(tasks),
                "tasks_alive": sum(1 for t in tasks if not t.done()),
                "exchange": instance.exchange_slug,
            }
        for name in self._registry:
            if name not in result:
                result[name] = {"running": False, "registered_only": True}
        return result
