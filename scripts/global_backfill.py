"""
scripts/global_backfill.py — Global 30-day backfill for all tokens and exchanges.

Usage:
    python -m scripts.global_backfill
"""

import asyncio
import logging
import sys
import os

# Add project root to path
sys.path.append(os.getcwd())

from app.config import get_settings
from app.core.database import init_db, close_db
from app.core.redis import init_redis, get_redis, close_redis
from app.collectors.extended import ExtendedCollector
from app.collectors.pacifica import PacificaCollector
from app.services.backfill_service import BackfillService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("global_backfill")

async def main():
    settings = get_settings()
    
    logger.info("Initializing database and redis...")
    await init_db(settings)
    await init_redis(settings)
    
    redis = get_redis()
    hl_collector = HyperliquidCollector(redis)
    aster_collector = AsterCollector(redis)
    extended_collector = ExtendedCollector(redis)
    pacifica_collector = PacificaCollector(redis)
    
    # We need to manually start/initialize them so they load their asset universes
    # Hyperliquid loads meta on first history call, but Aster needs start()
    logger.info("Initializing collectors...")
    await aster_collector.start()
    await extended_collector.start()
    await pacifica_collector.start()
    # Hyperliquid doesn't need a full start (which starts loops), 
    # just the session for REST calls which collector._fetch_history_range handles.
    
    collectors = [hl_collector, aster_collector, extended_collector, pacifica_collector]
    
    logger.info("Starting global 30-day backfill for all collectors...")
    # We use run_all which handles concurrency and 23h guards
    # If you want to FORCE it even if run recently, we'd need to clear Redis keys,
    # but for a global run, 30 days is what we want.
    await backfill_service.run_all(collectors, days=30)
    
    logger.info("Global backfill finished. Cleaning up...")
    
    await aster_collector.stop()
    await extended_collector.stop()
    await pacifica_collector.stop()
    # hl doesn't have background tasks yet as we didn't call start()
    
    await close_redis()
    await close_db()
    logger.info("Done.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(f"Global backfill failed: {e}", exc_info=True)
        sys.exit(1)
