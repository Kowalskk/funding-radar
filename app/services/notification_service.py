"""
app/services/notification_service.py — Checks arb opportunities against user
alert rules and dispatches Telegram notifications.

Flow:
  APScheduler (every 30s)
    → NotificationService.check_and_notify()
      → reads "arbitrage:current" from Redis
      → loads active NotificationRules from PostgreSQL (cached 5 min)
      → for each rule that matches:
          • throttle: max 1 alert per (user_id, token) every 30 min (Redis key)
          • calls TelegramSender.send_alert(chat_id, opportunity)

Only users with tier == "pro" or "custom" receive notifications.
Free users have no notification rules persisted (enforced at rule-creation time).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp
from redis.asyncio import Redis

from app.core.redis import get_redis

logger = logging.getLogger(__name__)

# ── Throttle key format ───────────────────────────────────────────────────────
# Redis key: notif:throttle:{user_id}:{token}
# TTL: 1800s (30 min)
_THROTTLE_TTL = 1800
_RULE_CACHE_TTL = 300   # cache DB rules for 5 min
_RULE_CACHE_KEY = "notif:rules_cache"


# ── Telegram message formatter ────────────────────────────────────────────────

def _format_alert(token: str, opp: dict, win: dict = None) -> str:
    """Build the Telegram markdown message for one arbitrage opportunity."""
    long_leg: dict = opp.get("long_leg", {})
    short_leg: dict = opp.get("short_leg", {})

    net_apr = opp.get("net_apr_taker", opp.get("funding_delta_apr", 0))
    price_spread = opp.get("price_spread_pct", 0)
    long_oi = long_leg.get("open_interest_usd", 0)
    short_oi = short_leg.get("open_interest_usd", 0)
    min_oi = min(long_oi, short_oi)
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    trend_section = ""
    if win:
        apr_1h = win.get("net_apr_1h")
        apr_24h = win.get("net_apr_24h")
        if apr_1h is not None and apr_24h is not None:
            status = "🔥 *Spiking*" if apr_1h > apr_24h * 1.2 else "✅ *Stable*"
            trend_section = (
                f"📊 *Trend*: {status}\n"
                f"• 1h Avg: `{apr_1h:.2f}%` ⚡\n"
                f"• 24h Avg: `{apr_24h:.2f}%` 🕒\n\n"
            )

    return (
        f"🔔 *Arbitrage Alert: {token}*\n"
        f"\n"
        f"📈 *Live APR*: `{net_apr:.2f}%` (Net)\n"
        f"\n"
        f"{trend_section}"
        f"📍 Long:  `{long_leg.get('exchange', '?').title()}`\n"
        f"📍 Short: `{short_leg.get('exchange', '?').title()}`\n"
        f"\n"
        f"💰 Spread: `{price_spread:.3f}%`\n"
        f"📊 Min OI: `${min_oi:,.0f}`\n"
        f"\n"
        f"⏰ {ts}"
    )


# ── Telegram HTTP sender ──────────────────────────────────────────────────────

class TelegramSender:
    """Thin async wrapper around the Telegram Bot API sendMessage endpoint.

    Non-blocking: errors are logged but never propagate to the caller.
    """

    def __init__(self, bot_token: str) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            connector=aiohttp.TCPConnector(limit=5),
        )

    async def stop(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def send(self, chat_id: str, text: str) -> bool:
        """Send a Markdown-formatted message. Returns True on success."""
        if not self._session or self._session.closed:
            logger.warning("TelegramSender: session not open — dropped message to %s", chat_id)
            return False
        try:
            async with self._session.post(
                self._url,
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        "Telegram API error %d for chat %s: %s", resp.status, chat_id, body[:200]
                    )
                    return False
                return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Telegram send error for chat %s: %s", chat_id, exc)
            return False


# ── Notification service ──────────────────────────────────────────────────────

class NotificationService:
    """Checks current arbitrage opportunities against active notification rules
    and sends Telegram alerts to eligible users.

    Called by APScheduler every 30 seconds via
    ``NotificationService.check_and_notify()``.
    """

    def __init__(self, sender: TelegramSender) -> None:
        self._sender = sender
        self._redis: Redis | None = None

    @property
    def redis(self) -> Redis:
        if self._redis is None:
            self._redis = get_redis()
        return self._redis

    # ── Entry point (called by scheduler) ────────────────────────────────────

    async def check_and_notify(self) -> None:
        """Main check loop — read arb opportunities and match against rules."""
        try:
            opportunities = await self._get_opportunities()
            if not opportunities:
                return

            rules = await self._get_rules_cached()
            if not rules:
                return

            await self._process_rules(rules, opportunities)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("NotificationService.check_and_notify error: %s", exc, exc_info=True)

    # ── Opportunities ──────────────────────────────────────────────────────────

    async def _get_opportunities(self) -> list[dict]:
        raw = await self.redis.get("arbitrage:current")
        if not raw:
            return []
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []

    # ── Rules (DB with Redis cache) ────────────────────────────────────────────

    async def _get_rules_cached(self) -> list[dict]:
        """Load NotificationRules from Redis cache; fall back to DB on miss."""
        cached = await self.redis.get(_RULE_CACHE_KEY)
        if cached:
            try:
                return json.loads(cached)
            except json.JSONDecodeError:
                pass

        rules = await self._load_rules_from_db()
        if rules:
            await self.redis.set(
                _RULE_CACHE_KEY, json.dumps(rules), ex=_RULE_CACHE_TTL
            )
        return rules

    async def _load_rules_from_db(self) -> list[dict]:
        """Query active notification rules for pro/custom users."""
        try:
            from app.core.database import get_db_session
            from app.models.db.user import NotificationRule, User, UserTier
            from sqlalchemy import select

            async with get_db_session() as session:
                result = await session.execute(
                    select(
                        NotificationRule.id,
                        NotificationRule.user_id,
                        NotificationRule.token_symbol,
                        NotificationRule.min_apr,
                        NotificationRule.exchanges,
                        User.telegram_chat_id,
                        User.tier,
                    )
                    .join(User, NotificationRule.user_id == User.id)
                    .where(
                        NotificationRule.is_active.is_(True),
                        User.is_active.is_(True),
                        User.telegram_chat_id.is_not(None),
                        User.tier.in_([UserTier.PRO, UserTier.CUSTOM]),
                    )
                )
                rows = result.all()

            return [
                {
                    "rule_id": row.id,
                    "user_id": row.user_id,
                    "token": row.token_symbol,
                    "min_apr": float(row.min_apr),
                    "exchanges": row.exchanges or [],
                    "chat_id": row.telegram_chat_id,
                }
                for row in rows
            ]
        except Exception as exc:
            logger.error("Failed to load notification rules from DB: %s", exc)
            return []

    # ── Matching and dispatch ──────────────────────────────────────────────────

    async def _process_rules(
        self, rules: list[dict], opportunities: list[dict]
    ) -> None:
        from app.processors.apr_windows import APRWindowHelper
        helper = APRWindowHelper(self.redis)

        # Build token → opportunity index for O(1) lookup
        opp_by_token: dict[str, dict] = {
            o.get("token", ""): o for o in opportunities
        }

        tasks = []
        for rule in rules:
            token: str = rule["token"]
            opp = opp_by_token.get(token)
            if opp is None:
                continue  # no arb data for this token

            net_apr = opp.get("net_apr_taker", opp.get("funding_delta_apr", 0))
            if net_apr < rule["min_apr"]:
                continue  # below threshold

            # Exchange filter (empty list = all exchanges)
            if rule["exchanges"]:
                involved = {
                    opp.get("long_leg", {}).get("exchange", ""),
                    opp.get("short_leg", {}).get("exchange", ""),
                }
                if not involved.intersection({e.lower() for e in rule["exchanges"]}):
                    continue

            tasks.append(self._maybe_alert(rule, token, opp, helper))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _maybe_alert(self, rule: dict, token: str, opp: dict, helper: APRWindowHelper) -> None:
        """Send an alert if the throttle window has not fired yet."""
        throttle_key = f"notif:throttle:{rule['user_id']}:{token}"

        # SET NX with 30-min TTL — only succeeds if not already set
        acquired = await self.redis.set(throttle_key, "1", ex=_THROTTLE_TTL, nx=True)
        if not acquired:
            return  # already notified within throttle window

        # Fetch windows for trend analysis
        win = await helper.get_pair_windows(
            opp["long_leg"]["exchange"],
            opp["short_leg"]["exchange"],
            token
        )

        msg = _format_alert(token, opp, win)
        sent = await self._sender.send(rule["chat_id"], msg)
        if sent:
            logger.info(
                "Alert sent: user=%s token=%s net_apr=%.2f%%",
                rule["user_id"], token, opp.get("net_apr_taker", 0)
            )
        else:
            # Release throttle so we can retry next cycle
            await self.redis.delete(throttle_key)

    # ── Rule cache invalidation ────────────────────────────────────────────────

    async def invalidate_rules_cache(self) -> None:
        """Call after a rule is created/updated/deleted so the next check reloads."""
        await self.redis.delete(_RULE_CACHE_KEY)
