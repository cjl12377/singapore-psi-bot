import asyncio
import logging
import math
import os
import secrets
import time

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import alerts
import analytics
from location import locate
from psi import format_psi_message, format_region_psi_message, get_psi_data

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


COMMANDS_TEXT = (
    "/psi — current PSI across Singapore\n"
    "/location — PSI for where you are (or just send a location)\n"
    "/alert — get notified when air quality changes\n"
    "/help — show this message"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Singapore PSI Bot\n\n"
        "Get live air quality readings from the National Environment Agency.\n\n"
        f"{COMMANDS_TEXT}\n\n"
        "Data from data.gov.sg, updated hourly."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"{COMMANDS_TEXT}\n\n"
        "Readings are fetched from data.gov.sg and cached for 10 minutes."
    )


async def cmd_psi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _deliver_psi(update, context, place=None)


async def cmd_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Share my location", request_location=True)]],
        one_time_keyboard=True,
        resize_keyboard=True,
    )
    await update.message.reply_text(
        "Tap the button below to share your location — I'll use it once to find "
        "your PSI region and won't store it.\n\n"
        "On desktop? The button only works in the phone app — use 📎 → Location instead.",
        reply_markup=keyboard,
    )


async def on_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Location message received")  # never log the coordinates
    loc = update.message.location
    place = locate(loc.latitude, loc.longitude)
    if place is None:
        await update.message.reply_text(
            "That doesn't look like it's in Singapore — PSI readings only cover Singapore.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    await _deliver_psi(update, context, place=place)


async def _deliver_psi(
    update: Update, context: ContextTypes.DEFAULT_TYPE, place: tuple[str, str] | None
) -> None:
    user_id = str(update.effective_user.id)
    now = time.monotonic()
    _prune_stale_requests(now)

    wait = USER_COOLDOWN_SECS - (now - _user_last_request.get(user_id, 0))
    if wait > 0:
        wait_int = math.ceil(wait)
        sent = await update.message.reply_text(
            f"⏳ Please wait {wait_int}s before requesting again.",
            reply_markup=ReplyKeyboardRemove(),
        )
        context.application.create_task(
            _countdown_and_delete(context.bot, sent.chat_id, sent.message_id, wait_int)
        )
        return

    _user_last_request[user_id] = now
    await analytics.track_request(user_id)

    data, stale_reason = await get_psi_data()
    if data is None:
        await update.message.reply_text(
            f"Could not fetch PSI data: {stale_reason}. Please try again later.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    text = (
        format_psi_message(data, stale_reason)
        if place is None
        else format_region_psi_message(data, *place, stale_reason)
    )
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardRemove()
    )


ALERT_EXPLAINER = (
    "I'll message you whenever Singapore's PSI category changes "
    "(e.g. Moderate → Unhealthy), with NEA's advice for that level."
)


def _alert_status(on: bool, extra: str = "") -> tuple[str, InlineKeyboardMarkup]:
    if on:
        text = f"🔔 Alerts are <b>on</b>.\n\n{ALERT_EXPLAINER}{extra}"
        button = InlineKeyboardButton("🔕 Turn off alerts", callback_data="alt:off")
    else:
        text = f"🔕 Alerts are <b>off</b>.\n\n{ALERT_EXPLAINER}{extra}"
        button = InlineKeyboardButton("🔔 Turn on alerts", callback_data="alt:on")
    return text, InlineKeyboardMarkup([[button]])


async def cmd_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    on = await alerts.is_subscribed(str(update.effective_user.id))
    text, markup = _alert_status(on)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def on_alert_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = str(update.effective_user.id)

    if query.data == "alt:off":
        await alerts.unsubscribe(user_id)
        text, markup = _alert_status(False)
    elif query.data == "alt:on":
        reading = await alerts.current_reading()
        if reading is None:
            await query.edit_message_text("Couldn't reach data.gov.sg just now — please try /alert again in a bit.")
            return
        category, value, _ = reading
        await alerts.subscribe(user_id, query.message.chat_id, category)
        text, markup = _alert_status(True, f"\n\nRight now: <b>{category}</b> (PSI {value}).")
    else:
        return

    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


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
    app.add_handler(CommandHandler("location", cmd_location))
    # UpdateType.MESSAGE excludes live-location edits, which arrive as
    # edited_message updates with update.message set to None.
    app.add_handler(MessageHandler(filters.LOCATION & filters.UpdateType.MESSAGE, on_location))
    app.add_handler(CommandHandler("alert", cmd_alert))
    app.add_handler(CallbackQueryHandler(on_alert_button, pattern=r"^alt:"))
    app.job_queue.run_repeating(alerts.run_alert_check, interval=1800, first=60)

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
