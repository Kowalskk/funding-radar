"""
app/services/payment_service.py — Stripe checkout + webhook handling.

Stripe events handled:
  checkout.session.completed     → upgrade user tier to PRO
  customer.subscription.deleted  → downgrade user tier to FREE
  customer.subscription.updated  → handle plan changes (future)
"""

from __future__ import annotations

import logging
from typing import Any

import stripe
from stripe import WebhookSignatureVerificationError

from app.config import get_settings

logger = logging.getLogger(__name__)


def _stripe_client() -> stripe.StripeClient:
    s = get_settings()
    if not s.stripe_secret_key:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured.")
    return stripe.StripeClient(s.stripe_secret_key.get_secret_value())


# ── Checkout ──────────────────────────────────────────────────────────────────

async def create_checkout_session(
    user_id: int,
    user_email: str,
    stripe_customer_id: str | None,
    success_url: str,
    cancel_url: str,
) -> dict[str, str]:
    """Create a Stripe Checkout Session for the Pro subscription.

    Returns ``{"url": "https://checkout.stripe.com/..."}``
    """
    s = get_settings()
    if not s.stripe_price_id_pro:
        raise RuntimeError("STRIPE_PRICE_ID_PRO is not configured.")

    client = _stripe_client()

    session_params: dict[str, Any] = {
        "mode": "subscription",
        "line_items": [{"price": s.stripe_price_id_pro, "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": {"user_id": str(user_id)},
        "allow_promotion_codes": True,
    }

    if stripe_customer_id:
        session_params["customer"] = stripe_customer_id
    else:
        session_params["customer_email"] = user_email

    session = await _run_sync(
        lambda: client.checkout.sessions.create(params=session_params)
    )
    return {"url": session.url}


# ── Webhook ───────────────────────────────────────────────────────────────────

async def handle_stripe_webhook(
    payload: bytes,
    stripe_signature: str,
) -> str:
    """Verify and dispatch a Stripe webhook event.

    Returns the event type string on success.
    Raises ``stripe.WebhookSignatureVerificationError`` on bad signature.
    """
    s = get_settings()
    if not s.stripe_webhook_secret:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET is not configured.")

    secret = s.stripe_webhook_secret.get_secret_value()

    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, secret)
    except WebhookSignatureVerificationError as exc:
        logger.warning("Stripe webhook signature verification failed: %s", exc)
        raise

    event_type: str = event["type"]
    logger.info("Stripe webhook received: %s", event_type)

    if event_type == "checkout.session.completed":
        await _handle_checkout_completed(event["data"]["object"])
    elif event_type == "customer.subscription.deleted":
        await _handle_subscription_deleted(event["data"]["object"])
    elif event_type == "customer.subscription.updated":
        await _handle_subscription_updated(event["data"]["object"])

    return event_type


# ── Event handlers ────────────────────────────────────────────────────────────

async def _handle_checkout_completed(session: dict) -> None:
    """Upgrade user to PRO after successful checkout."""
    user_id_str = (session.get("metadata") or {}).get("user_id")
    customer_id = session.get("customer")

    if not user_id_str:
        logger.warning("checkout.session.completed missing metadata.user_id")
        return

    try:
        user_id = int(user_id_str)
        await _upgrade_user(user_id, customer_id)
        logger.info("Upgraded user %d to PRO (Stripe customer: %s)", user_id, customer_id)
    except Exception as exc:
        logger.error("Failed to upgrade user %s: %s", user_id_str, exc, exc_info=True)


async def _handle_subscription_deleted(subscription: dict) -> None:
    """Downgrade user to FREE when subscription is cancelled."""
    customer_id = subscription.get("customer")
    if not customer_id:
        return
    try:
        await _downgrade_user_by_customer(customer_id)
        logger.info("Downgraded Stripe customer %s to FREE.", customer_id)
    except Exception as exc:
        logger.error("Failed to downgrade customer %s: %s", customer_id, exc, exc_info=True)


async def _handle_subscription_updated(subscription: dict) -> None:
    """Handle plan changes — currently a no-op placeholder."""
    logger.debug("customer.subscription.updated: %s", subscription.get("id"))


# ── DB helpers ─────────────────────────────────────────────────────────────────

async def _upgrade_user(user_id: int, stripe_customer_id: str | None) -> None:
    from app.core.database import get_db_session
    from app.models.db.user import User, UserTier
    from sqlalchemy import update as sql_update

    update_vals: dict = {"tier": UserTier.PRO}
    if stripe_customer_id:
        update_vals["stripe_customer_id"] = stripe_customer_id

    async with get_db_session() as session:
        await session.execute(
            sql_update(User).where(User.id == user_id).values(**update_vals)
        )
        await session.commit()

    # Bust the Redis auth cache for this user so the new tier is reflected immediately
    await _bust_user_tier_cache(user_id)


async def _downgrade_user_by_customer(stripe_customer_id: str) -> None:
    from app.core.database import get_db_session
    from app.models.db.user import User, UserTier
    from sqlalchemy import update as sql_update, select

    async with get_db_session() as session:
        user_id_row = await session.scalar(
            select(User.id).where(User.stripe_customer_id == stripe_customer_id)
        )
        if user_id_row is None:
            logger.warning("No user found for Stripe customer %s", stripe_customer_id)
            return
        await session.execute(
            sql_update(User)
            .where(User.stripe_customer_id == stripe_customer_id)
            .values(tier=UserTier.FREE)
        )
        await session.commit()

    await _bust_user_tier_cache(user_id_row)


async def _bust_user_tier_cache(user_id: int) -> None:
    """Invalidate Redis auth:tier cache keys for this user."""
    try:
        from app.core.redis import get_redis
        redis = get_redis()
        # Scan for keys matching auth:tier:* and delete those with user_id in value
        # (Simpler approach: delete all tier cache on upgrade — small perf cost)
        # For precision, the cache key is auth:tier:{api_key} so we'd need a
        # reverse lookup. Instead, we keep a secondary key: auth:user_api_key:{user_id}
        api_key = await redis.get(f"auth:user_api_key:{user_id}")
        if api_key:
            key = api_key if isinstance(api_key, str) else api_key.decode()
            await redis.delete(f"auth:tier:{key}")
    except Exception as exc:
        logger.warning("Could not bust tier cache for user %d: %s", user_id, exc)


# ── Sync helper ───────────────────────────────────────────────────────────────

import asyncio


async def _run_sync(fn):
    """Run a synchronous Stripe SDK call in the default thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fn)
