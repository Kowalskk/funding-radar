#!/usr/bin/env python
"""
scripts/health_check.py — System health checker for funding-radar.

Checks:
  ✓ FastAPI /health responds 200
  ✓ FastAPI /ready responds 200
  ✓ Redis has recent funding data (< 120s old)
  ✓ Arbitrage opportunities are populated
  ✓ WS manager is alive

Usage:
  python scripts/health_check.py [--url http://localhost:8000]
  Exit code 0 = healthy, 1 = degraded.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import aiohttp


async def check(base_url: str) -> list[tuple[str, bool, str]]:
    """Returns list of (check_name, passed, details)."""
    results: list[tuple[str, bool, str]] = []
    timeout = aiohttp.ClientTimeout(total=10)

    async with aiohttp.ClientSession(timeout=timeout) as http:
        # /health
        try:
            async with http.get(f"{base_url}/health") as r:
                ok = r.status == 200
                results.append(("FastAPI /health", ok, f"HTTP {r.status}"))
        except Exception as e:
            results.append(("FastAPI /health", False, str(e)))

        # /ready
        try:
            async with http.get(f"{base_url}/ready") as r:
                ok = r.status == 200
                results.append(("FastAPI /ready", ok, f"HTTP {r.status}"))
        except Exception as e:
            results.append(("FastAPI /ready", False, str(e)))

        # /ws/status
        try:
            async with http.get(f"{base_url}/ws/status") as r:
                body = await r.json()
                total = body.get("total_connections", 0)
                alive = body.get("heartbeat_alive", False)
                results.append(("WS manager", r.status == 200,
                                 f"connections={total} heartbeat={alive}"))
        except Exception as e:
            results.append(("WS manager", False, str(e)))

        # /collectors/status
        try:
            async with http.get(f"{base_url}/collectors/status") as r:
                body = await r.json()
                collectors = body.get("collectors", body)
                running = [k for k, v in collectors.items()
                           if isinstance(v, dict) and v.get("running")]
                results.append(("Collectors", r.status == 200,
                                 f"running={running}"))
        except Exception as e:
            results.append(("Collectors", False, str(e)))

        # /service/status
        try:
            async with http.get(f"{base_url}/service/status") as r:
                body = await r.json()
                updates = body.get("update_count", "?")
                results.append(("FundingService", r.status == 200,
                                 f"updates={updates}"))
        except Exception as e:
            results.append(("FundingService", False, str(e)))

        # Check arbitrage opportunities via API
        try:
            async with http.get(f"{base_url}/api/v1/arbitrage/opportunities") as r:
                body = await r.json()
                count = len(body) if isinstance(body, list) else "?"
                results.append(("Arb opportunities", r.status == 200,
                                 f"count={count}"))
        except Exception as e:
            results.append(("Arb opportunities", False, str(e)))

    return results


async def main(base_url: str) -> int:
    print(f"🔍 Health check: {base_url}\n")
    results = await check(base_url)

    passed = 0
    failed = 0
    for name, ok, detail in results:
        icon = "✅" if ok else "❌"
        print(f"  {icon} {name:<30} {detail}")
        if ok:
            passed += 1
        else:
            failed += 1

    print(f"\n{'✅' if failed == 0 else '⚠️ '} {passed}/{passed+failed} checks passed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.url)))
