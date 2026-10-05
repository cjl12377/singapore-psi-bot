import asyncio
import json
import logging
import math
import os
import secrets
import time

import httpx
from telegram import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest
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
import prefs
from location import locate
from psi import (
    format_psi_caption,
    format_psi_message,
    format_psi_rich,
    format_region_psi_message,
    get_psi_data,
)
from psi_map import render_psi_map

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
    "/view — choose map or text for /psi\n"
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


MAP_MEDIA_ID = "map"


async def _send_rich(token: str, chat_id: int, markdown: str, png: bytes | None = None) -> bool:
    """Send via Bot API sendRichMessage (PTB 21.6 has no wrapper). False -> caller falls back.

    With png, the image is uploaded in the same request and the markdown must reference it
    as tg://photo?id=MAP_MEDIA_ID."""
    rich: dict = {"markdown": markdown}
    if png:
        rich["media"] = [{"id": MAP_MEDIA_ID,
                          "media": {"type": "photo", "media": "attach://map.png"}}]
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            payload = {"chat_id": chat_id,
                       "rich_message": json.dumps(rich, ensure_ascii=False)}
            if png:
                resp = await client.post(
                    f"https://api.telegram.org/bot{token}/sendRichMessage",
                    data=payload, files={"map.png": ("map.png", png, "image/png")},
                )
            else:
                resp = await client.post(
                    f"https://api.telegram.org/bot{token}/sendRichMessage", json=payload)
        if resp.status_code == 200 and resp.json().get("ok"):
            return True
        logger.warning("sendRichMessage rejected: %s", resp.text[:200])
    except Exception as exc:
        logger.warning("sendRichMessage failed: %s", type(exc).__name__)
    return False


async def _render_map(data: dict, area: str | None) -> bytes | None:
    try:
        readings = data["data"]["items"][0]["readings"]
        return await asyncio.to_thread(render_psi_map, readings["psi_twenty_four_hourly"], area,
                                       readings.get("pm25_one_hourly"))
    except Exception as exc:
        logger.warning("PSI map render failed: %s", type(exc).__name__)
        return None


async def _send_map(token: str, chat_id: int, png: bytes, caption: str) -> bool:
    """Send the regional map as a plain photo (sendPhoto). False -> caller falls back to the rich table."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                files={"photo": ("psi.png", png, "image/png")},
            )
        if resp.status_code == 200 and resp.json().get("ok"):
            return True
        logger.warning("sendPhoto rejected: %s", resp.text[:200])
    except Exception as exc:
        logger.warning("PSI map failed: %s", type(exc).__name__)
    return False


async def _deliver_psi(
    update: Update, context: ContextTypes.DEFAULT_TYPE, place: tuple[str, str] | None,
    preview: bool = False,
) -> None:
    user_id = str(update.effective_user.id)
    now = time.monotonic()
    if not preview:  # a /view preview isn't a fresh request: no cooldown or analytics
        _prune_stale_requests(now)

        wait = USER_COOLDOWN_SECS - (now - _user_last_request.get(user_id, 0))
        if wait > 0:
            wait_int = math.ceil(wait)
            sent = await update.effective_chat.send_message(
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
        await update.effective_chat.send_message(
            f"Could not fetch PSI data: {stale_reason}. Please try again later.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    area, region = place if place else (None, None)
    token, chat_id = context.bot.token, update.effective_chat.id
    wants_map = await prefs.get_view(user_id) == prefs.VIEW_MAP
    png = await _render_map(data, area) if wants_map else None
    if png:
        # 1) map embedded in a rich message; 2) map as a plain photo + caption
        if await _send_rich(token, chat_id,
                            format_psi_rich(data, stale_reason, area, region, map_id=MAP_MEDIA_ID), png):
            return
        if await _send_map(token, chat_id, png, format_psi_caption(data, stale_reason, area, region)):
            if place:  # photos can't carry the keyboard removal
                await update.effective_chat.send_message("👆 Your area is outlined on the map.",
                                                reply_markup=ReplyKeyboardRemove())
            return
    if await _send_rich(context.bot.token, update.effective_chat.id,
                        format_psi_rich(data, stale_reason, area, region)):
        return

    text = (
        format_psi_message(data, stale_reason)
        if place is None
        else format_region_psi_message(data, *place, stale_reason)
    )
    await update.effective_chat.send_message(
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
    if update.effective_user.id != ADMIN_USER_ID or update.effective_chat.type != ChatType.PRIVATE:
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


def _view_status(view: str) -> tuple[str, InlineKeyboardMarkup]:
    mark = lambda v: "✅ " if view == v else ""
    text = (
        "How should /psi look?\n\n"
        "🗺 <b>Map</b> — a map of Singapore with each region's PSI colour-coded.\n"
        "📝 <b>Text</b> — a plain table, lighter on data.\n\n"
        f"Currently: <b>{'Map' if view == prefs.VIEW_MAP else 'Text'}</b>"
    )
    return text, InlineKeyboardMarkup([[
        InlineKeyboardButton(f"{mark(prefs.VIEW_MAP)}🗺 Map", callback_data="view:map"),
        InlineKeyboardButton(f"{mark(prefs.VIEW_TEXT)}📝 Text", callback_data="view:text"),
    ]])


async def cmd_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, markup = _view_status(await prefs.get_view(str(update.effective_user.id)))
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def on_view_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    view = query.data.removeprefix("view:")
    if view not in (prefs.VIEW_MAP, prefs.VIEW_TEXT):
        await query.answer()
        return
    try:
        await prefs.set_view(str(update.effective_user.id), view)
    except Exception as exc:
        logger.warning("view preference write failed: %s", type(exc).__name__)
        await query.answer("Couldn't save that — try again in a bit.", show_alert=True)
        return
    await query.answer("Saved")
    text, markup = _view_status(view)
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except BadRequest:  # tapping the already-selected button -> "message is not modified"; nothing changed, so no preview
        return
    await _deliver_psi(update, context, None, preview=True)


async def _post_init(app: Application) -> None:
    # /stats is deliberately absent so it never shows in Telegram's command menu.
    commands = [
        BotCommand("psi", "Current PSI across Singapore"),
        BotCommand("location", "PSI for where you are"),
        BotCommand("alert", "Get notified when air quality changes"),
        BotCommand("view", "Choose map or text for /psi"),
        BotCommand("help", "Show available commands"),
        BotCommand("start", "About this bot"),
    ]
    # Telegram shows the most specific scope that has commands, so stale lists in
    # narrower scopes hide the default one. Clear them and set the ones users see.
    for scope in (BotCommandScopeAllGroupChats(), BotCommandScopeAllChatAdministrators()):
        await app.bot.delete_my_commands(scope=scope)
    await app.bot.set_my_commands(commands)
    await app.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
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
    app.add_handler(CommandHandler("view", cmd_view))
    app.add_handler(CallbackQueryHandler(on_view_button, pattern=r"^view:"))
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
