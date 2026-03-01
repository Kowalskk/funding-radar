"""
app/api/v1/router.py — Central API v1 router.

Mounts all sub-routers under /api/v1.
"""

from fastapi import APIRouter

from app.api.v1 import arbitrage, auth, exchanges, funding, simulator, webhooks

api_v1_router = APIRouter(prefix="/api/v1")

api_v1_router.include_router(auth.router)
api_v1_router.include_router(funding.router)
api_v1_router.include_router(arbitrage.router)
api_v1_router.include_router(simulator.router)
api_v1_router.include_router(exchanges.router)
api_v1_router.include_router(webhooks.router)
