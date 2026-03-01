"""
app/bot/telegram_bot.py — Telegram Bot command handler (polling mode).

Commands
────────
  /start              → register chat_id with the calling user's account
  /alerts             → list active NotificationRules for this chat
  /setalert {token} {min_apr} [exchanges...]  → create a new rule
  /removealert {id}   → deactivate a rule by ID
  /status             → show live exchange stats + last data update time

Integration
───────────
The bot runs in long-polling mode using python-telegram-bot's
ApplicationBuilder.  It is started as a background asyncio task so it
does not block the main FastAPI event loop.

python-telegram-bot v20+ is fully async; no thread pool is needed.

Error handling
──────────────
All handlers log errors and reply gracefully — they never raise, ensuring
the polling loop stays alive on Telegram API transient errors.
"""

from __future__ import annotations

import asyncio
import json
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from app.config import get_settings
from app.core.redis import get_redis
from app.services.notification_service import NotificationService

logger = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────


async def _get_user_by_chat_id(chat_id: str):
    """Return the User ORM object for a given Telegram chat_id, or None."""
    from app.core.database import get_db_session
    from app.models.db.user import User
    from sqlalchemy import select

    async with get_db_session() as session:
        return await session.scalar(
            select(User).where(User.telegram_chat_id == chat_id, User.is_active.is_(True))
        )


def _escape_md(text: str) -> str:
    """Minimally escape characters that break Telegram Markdown."""
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


# ── Command handlers ──────────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — link this Telegram chat_id to the user account."""
    chat_id = str(update.effective_chat.id)

    # Optionally accept an API key as argument: /start <api_key>
    args = context.args or []
    api_key = args[0] if args else None

    if not api_key:
        await update.message.reply_text(
            "👋 Welcome to *Funding Radar*!\n\n"
            "To link your account, send:\n"
            "`/start <your_api_key>`\n\n"
            "Find your API key in the Funding Radar dashboard.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        from app.core.database import get_db_session
        from app.models.db.user import User
        from sqlalchemy import select, update as sql_update

        async with get_db_session() as session:
            user = await session.scalar(
                select(User).where(User.api_key == api_key, User.is_active.is_(True))
            )
            if user is None:
                await update.message.reply_text(
                    "❌ Invalid API key. Check your dashboard and try again."
                )
                return

            await session.execute(
                sql_update(User)
                .where(User.id == user.id)
                .values(telegram_chat_id=chat_id)
            )
            await session.commit()

        # Invalidate notification rule cache so new chat_id is used
        redis = get_redis()
        await redis.delete("notif:rules_cache")

        await update.message.reply_text(
            f"✅ Account linked!\n\n"
            f"Tier: *{user.tier.value.upper()}*\n"
            f"You'll receive arbitrage alerts here.\n\n"
            f"Use /alerts to see your active alert rules.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        logger.error("cmd_start error: %s", exc, exc_info=True)
        await update.message.reply_text("⚠️ Something went wrong. Try again later.")


async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/alerts — list active rules for this Telegram account."""
    chat_id = str(update.effective_chat.id)
    try:
        from app.core.database import get_db_session
        from app.models.db.user import NotificationRule, User
        from sqlalchemy import select

        async with get_db_session() as session:
            result = await session.execute(
                select(NotificationRule)
                .join(User, NotificationRule.user_id == User.id)
                .where(
                    User.telegram_chat_id == chat_id,
                    NotificationRule.is_active.is_(True),
                )
                .order_by(NotificationRule.id)
            )
            rules = result.scalars().all()

        if not rules:
            await update.message.reply_text(
                "You have no active alert rules.\n"
                "Use `/setalert BTC 20` to create one.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        lines = ["*Your active alerts:*\n"]
        for r in rules:
            exch = ", ".join(r.exchanges) if r.exchanges else "all exchanges"
            lines.append(f"  `#{r.id}` — *{r.token_symbol}* ≥ `{float(r.min_apr):.1f}%` APR ({exch})")

        await update.message.reply_text(
            "\n".join(lines) + "\n\nUse `/removealert <id>` to delete a rule.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        logger.error("cmd_alerts error: %s", exc, exc_info=True)
        await update.message.reply_text("⚠️ Could not load alerts. Try again later.")


async def cmd_setalert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setalert {TOKEN} {min_apr} [exchange1 exchange2 ...]"""
    chat_id = str(update.effective_chat.id)
    args = context.args or []

    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/setalert {TOKEN} {min_apr} [exchanges...]`\n"
            "Example: `/setalert BTC 20 hyperliquid aster`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    token = args[0].upper()
    try:
        min_apr = float(args[1])
    except ValueError:
        await update.message.reply_text("❌ `min_apr` must be a number (e.g. 20.5).")
        return

    if not (0.0 <= min_apr <= 1000.0):
        await update.message.reply_text("❌ `min_apr` must be between 0 and 1000.")
        return

    exchanges = [e.lower() for e in args[2:]]

    try:
        from app.core.database import get_db_session
        from app.models.db.user import NotificationRule, User, UserTier
        from sqlalchemy import select, func

        async with get_db_session() as session:
            user = await session.scalar(
                select(User).where(
                    User.telegram_chat_id == chat_id, User.is_active.is_(True)
                )
            )
            if user is None:
                await update.message.reply_text(
                    "❌ Account not linked. Use `/start <api_key>` first.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return

            if user.tier not in (UserTier.PRO, UserTier.CUSTOM):
                await update.message.reply_text(
                    "⚠️ Telegram alerts are a *Pro* feature.\n"
                    "Upgrade your plan at funding-radar.io/upgrade.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return

            # Limit: 20 active rules per user
            count = await session.scalar(
                select(func.count(NotificationRule.id)).where(
                    NotificationRule.user_id == user.id,
                    NotificationRule.is_active.is_(True),
                )
            )
            if (count or 0) >= 20:
                await update.message.reply_text(
                    "❌ Maximum 20 active alert rules per account."
                )
                return

            rule = NotificationRule(
                user_id=user.id,
                token_symbol=token,
                min_apr=min_apr,
                exchanges=exchanges,
                is_active=True,
            )
            session.add(rule)
            await session.commit()
            await session.refresh(rule)

        # Invalidate rule cache
        await get_redis().delete("notif:rules_cache")

        exch_str = ", ".join(exchanges) if exchanges else "all exchanges"
        await update.message.reply_text(
            f"✅ Alert created `#{rule.id}`\n"
            f"Token: *{token}* | Min APR: `{min_apr:.1f}%` | Exchanges: {exch_str}",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        logger.error("cmd_setalert error: %s", exc, exc_info=True)
        await update.message.reply_text("⚠️ Could not create alert. Try again later.")


async def cmd_removealert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/removealert {id}"""
    chat_id = str(update.effective_chat.id)
    args = context.args or []

    if not args:
        await update.message.reply_text("Usage: `/removealert <id>`", parse_mode=ParseMode.MARKDOWN)
        return

    try:
        rule_id = int(args[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text("❌ Rule ID must be a number.")
        return

    try:
        from app.core.database import get_db_session
        from app.models.db.user import NotificationRule, User
        from sqlalchemy import select, update as sql_update

        async with get_db_session() as session:
            # Verify ownership
            rule = await session.scalar(
                select(NotificationRule)
                .join(User, NotificationRule.user_id == User.id)
                .where(
                    NotificationRule.id == rule_id,
                    User.telegram_chat_id == chat_id,
                    NotificationRule.is_active.is_(True),
                )
            )
            if rule is None:
                await update.message.reply_text(
                    f"❌ Alert `#{rule_id}` not found or already removed.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return

            await session.execute(
                sql_update(NotificationRule)
                .where(NotificationRule.id == rule_id)
                .values(is_active=False)
            )
            await session.commit()

        await get_redis().delete("notif:rules_cache")
        await update.message.reply_text(
            f"✅ Alert `#{rule_id}` removed.", parse_mode=ParseMode.MARKDOWN
        )
    except Exception as exc:
        logger.error("cmd_removealert error: %s", exc, exc_info=True)
        await update.message.reply_text("⚠️ Could not remove alert. Try again later.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — show live exchange data summary."""
    try:
        redis = get_redis()
        raw_ranked = await redis.get("funding:ranked")
        raw_arb = await redis.get("arbitrage:current")

        ranked: list[dict] = json.loads(raw_ranked) if raw_ranked else []
        arb: list[dict] = json.loads(raw_arb) if raw_arb else []

        token_count = len(ranked)
        arb_count = len(arb)

        exchange_token_counts: dict[str, int] = {}
        for item in ranked:
            for row in item.get("rows", []):
                ex = row.get("exchange", "?")
                exchange_token_counts[ex] = exchange_token_counts.get(ex, 0) + 1

        lines = [f"📡 *Funding Radar Status*\n"]
        lines.append(f"Tokens tracked: `{token_count}`")
        lines.append(f"Arb opportunities: `{arb_count}`\n")

        if exchange_token_counts:
            lines.append("*Exchanges:*")
            for ex, n in sorted(exchange_token_counts.items(), key=lambda x: -x[1]):
                lines.append(f"  • `{ex}`: {n} tokens")

        if arb and arb[0]:
            best = arb[0]
            lines.append(
                f"\n🏆 Best opp: *{best.get('token')}* — "
                f"`{best.get('net_apr_taker', 0):.2f}%` APR net"
            )

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    except Exception as exc:
        logger.error("cmd_status error: %s", exc, exc_info=True)
        await update.message.reply_text("⚠️ Could not fetch status. Try again later.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Telegram bot error: %s", context.error, exc_info=context.error)


# ── Bot factory ───────────────────────────────────────────────────────────────


def build_bot_application(bot_token: str) -> Application:
    """Build and configure the python-telegram-bot Application."""
    app = ApplicationBuilder().token(bot_token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CommandHandler("setalert", cmd_setalert))
    app.add_handler(CommandHandler("removealert", cmd_removealert))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_error_handler(error_handler)

    return app


# ── Background runner ──────────────────────────────────────────────────────────


class TelegramBotRunner:
    """Wraps the python-telegram-bot Application for asyncio lifecycle management.

    Runs in long-polling mode as a background asyncio task.
    """

    def __init__(self, bot_token: str) -> None:
        self._bot_token = bot_token
        self._app: Application | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if not self._bot_token:
            logger.warning("TELEGRAM_BOT_TOKEN not set — bot disabled.")
            return

        self._app = build_bot_application(self._bot_token)
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(
            allowed_updates=["message"],
            drop_pending_updates=True,
        )
        logger.info("Telegram bot started (long-polling).")

    async def stop(self) -> None:
        if self._app is None:
            return
        logger.info("Stopping Telegram bot…")
        try:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        except Exception as exc:
            logger.error("Error stopping Telegram bot: %s", exc)
        self._app = None
        logger.info("Telegram bot stopped.")
