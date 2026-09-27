import asyncio
import logging
import math
import os
import secrets
import time

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

import analytics
from psi import format_psi_message, get_psi_data

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
)
# httpx logs full request URLs at INFO, and Telegram embeds the bot token
# in the URL path (https://api.telegram.org/bot<TOKEN>/...) — silence it
# to keep the token out of application logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"].rstrip("/")
PORT = int(os.environ.get("PORT", 8443))
WEBHOOK_SECRET = secrets.token_urlsafe(32)
ADMIN_USER_ID = int(os.environ["ADMIN_USER_ID"])

USER_COOLDOWN_SECS = 30
_user_last_request: dict[str, float] = {}


def _prune_stale_requests(now: float) -> None:
    cutoff = now - USER_COOLDOWN_SECS
    for uid in [u for u, ts in _user_last_request.items() if ts < cutoff]:
        del _user_last_request[uid]


async def _countdown_and_delete(bot, chat_id: int, message_id: int, seconds: int) -> None:
    """Ticks a cooldown message down to 0 once per second, then deletes it."""
    remaining = seconds
    while remaining > 0:
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            return
        remaining -= 1
        if remaining > 0:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=f"⏳ Please wait {remaining}s before requesting again.",
                )
            except Exception:
                pass  # rate-limited or message already gone — skip this tick
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Singapore PSI Bot\n\n"
        "Get live air quality readings from the National Environment Agency.\n\n"
        "/psi  — current PSI and PM2.5 readings\n"
        "/help — show this message\n\n"
        "Data from data.gov.sg, updated hourly."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/psi  — current PSI and PM2.5 readings (national + regional)\n"
        "/help — show this message\n\n"
        "Readings are fetched from data.gov.sg and cached for 10 minutes."
    )


async def cmd_psi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    now = time.monotonic()
    _prune_stale_requests(now)

    wait = USER_COOLDOWN_SECS - (now - _user_last_request.get(user_id, 0))
    if wait > 0:
        wait_int = math.ceil(wait)
        sent = await update.message.reply_text(f"⏳ Please wait {wait_int}s before requesting again.")
        context.application.create_task(
            _countdown_and_delete(context.bot, sent.chat_id, sent.message_id, wait_int)
        )
        return

    _user_last_request[user_id] = now
    await analytics.track_request(user_id)

    data, stale_reason = await get_psi_data()
    if data is None:
        await update.message.reply_text(
            f"Could not fetch PSI data: {stale_reason}. Please try again later."
        )
        return

    await update.message.reply_text(
        format_psi_message(data, stale_reason), parse_mode=ParseMode.HTML
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_USER_ID:
        return  # silent — indistinguishable from an unrecognized command

    active_24h = await analytics.active_users_24h()
    total_users = await analytics.total_unique_users()
    retained = await analytics.retained_users()
    growth = await analytics.daily_growth(7)

    growth_lines = "\n".join(f"{date[5:]}   +{count}" for date, count in growth)
    week_total = sum(count for _, count in growth)

    await update.message.reply_text(
        f"<b>Bot Analytics</b>\n\n"
        f"Active users (24h): <b>{active_24h}</b>\n"
        f"All-time unique users: <b>{total_users}</b>\n"
        f"Retained (4+ visits, >12h apart): <b>{retained}</b>\n\n"
        f"<b>New users, last 7 days</b>\n"
        f"<pre>{growth_lines}</pre>\n"
        f"Total this week: +{week_total}",
        parse_mode=ParseMode.HTML,
    )


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("psi", cmd_psi))
    app.add_handler(CommandHandler("stats", cmd_stats))

    logger.info("Starting webhook on port %d → %s/webhook", PORT, WEBHOOK_URL)
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=f"{WEBHOOK_URL}/webhook",
        secret_token=WEBHOOK_SECRET,
    )


if __name__ == "__main__":
    main()
