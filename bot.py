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
import feedback
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
_user_warned: set[str] = set()  # users already told about their current cooldown


def _prune_stale_requests(now: float) -> None:
    cutoff = now - USER_COOLDOWN_SECS
    for uid in [u for u, ts in _user_last_request.items() if ts < cutoff]:
        del _user_last_request[uid]
        _user_warned.discard(uid)


async def _delete_later(bot, chat_id: int, message_id: int, seconds: int) -> None:
    await asyncio.sleep(seconds)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass  # already deleted by the user


COMMANDS_TEXT = (
    "/psi — current PSI across Singapore\n"
    "/location — PSI for where you are (or just send a location)\n"
    "/alert — get notified when air quality changes\n"
    "/view — choose map or text for /psi\n"
    "/feedback — send feedback to the developer\n"
    "/help — show this message"
)


PSI_BUTTON = "🌫 Check PSI"
LOCATION_BUTTON = "📍 Share location (mobile)"  # the button does nothing on desktop apps

# Pinned under the message box in private chats. Telegram doesn't tell bots which
# device a user is on, so every client gets both buttons; on desktop the location
# button does nothing, and /location explains the 📎 → Location route instead.
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton(PSI_BUTTON), KeyboardButton(LOCATION_BUTTON, request_location=True)]],
    resize_keyboard=True,
    is_persistent=True,
)


def _keyboard(update: Update) -> ReplyKeyboardMarkup | None:
    """The button bar for private chats. Groups get none: a group keyboard shows to every
    member, and Telegram only allows location-request buttons in private chats."""
    return MAIN_KEYBOARD if update.effective_chat.type == ChatType.PRIVATE else None


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Singapore PSI Bot\n\n"
        "Get live air quality readings from the National Environment Agency.\n\n"
        f"{COMMANDS_TEXT}\n\n"
        "Data from data.gov.sg, updated hourly.",
        reply_markup=_keyboard(update),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"{COMMANDS_TEXT}\n\n"
        "Readings are fetched from data.gov.sg and cached for 10 minutes.",
        reply_markup=_keyboard(update),
    )


async def cmd_psi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _deliver_psi(update, context, place=None)


async def cmd_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text(
            "Location sharing only works in a private chat with me — message me directly.")
        return
    await update.message.reply_text(
        f"Tap {LOCATION_BUTTON} at the bottom of the chat — I'll use your location once to "
        "find your PSI region and won't store it.\n\n"
        "On desktop? The button only works in the phone app — use 📎 → Location instead.",
        reply_markup=MAIN_KEYBOARD,
    )


async def on_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Location message received")  # never log the coordinates
    loc = update.message.location
    place = locate(loc.latitude, loc.longitude)
    if place is None:
        await update.message.reply_text(
            "That doesn't look like it's in Singapore — PSI readings only cover Singapore.",
            reply_markup=_keyboard(update),
        )
        return
    await _deliver_psi(update, context, place=place)


MAP_MEDIA_ID = "map"


def _markup_param(markup: ReplyKeyboardMarkup | None) -> dict:
    """reply_markup as a raw Bot API parameter (JSON-encoded), or nothing."""
    return {"reply_markup": json.dumps(markup.to_dict(), ensure_ascii=False)} if markup else {}


async def _send_rich(token: str, chat_id: int, markdown: str, png: bytes | None = None,
                     reply_markup: ReplyKeyboardMarkup | None = None) -> bool:
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
                       "rich_message": json.dumps(rich, ensure_ascii=False),
                       **_markup_param(reply_markup)}
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


# Rendered maps for the current reading, keyed by highlighted area (None = no outline).
# At most 56 entries (55 planning areas + none); cleared whenever new data is fetched.
_map_cache: dict = {"data": None, "pngs": {}}


async def _render_map(data: dict, area: str | None) -> bytes | None:
    if _map_cache["data"] is not data:  # a new fetch: earlier maps show old readings
        _map_cache.update(data=data, pngs={})
    if area in _map_cache["pngs"]:
        return _map_cache["pngs"][area]
    try:
        readings = data["data"]["items"][0]["readings"]
        png = await asyncio.to_thread(render_psi_map, readings["psi_twenty_four_hourly"], area,
                                      readings.get("pm25_one_hourly"))
    except Exception as exc:
        logger.warning("PSI map render failed: %s", type(exc).__name__)
        return None
    if _map_cache["data"] is data:  # still current after the render
        _map_cache["pngs"][area] = png
    return png


async def _send_map(token: str, chat_id: int, png: bytes, caption: str,
                    reply_markup: ReplyKeyboardMarkup | None = None) -> bool:
    """Send the regional map as a plain photo (sendPhoto). False -> caller falls back to the rich table."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML",
                      **_markup_param(reply_markup)},
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
    _prune_stale_requests(now)

    wait = USER_COOLDOWN_SECS - (now - _user_last_request.get(user_id, 0))
    if wait > 0:
        # A /view preview inside the cooldown is skipped (the choice is still saved).
        # Otherwise one notice per cooldown; repeated attempts are ignored so spam
        # can't turn into a stream of outgoing messages.
        if not preview and user_id not in _user_warned:
            _user_warned.add(user_id)
            wait_int = math.ceil(wait)
            sent = await update.effective_chat.send_message(
                f"⏳ Please wait {wait_int}s before requesting again.",
                reply_markup=_keyboard(update),
            )
            context.application.create_task(
                _delete_later(context.bot, sent.chat_id, sent.message_id, wait_int)
            )
        return

    _user_last_request[user_id] = now
    _user_warned.discard(user_id)
    if not preview:  # a /view preview isn't a fresh request: no analytics
        await analytics.track_request(user_id)

    data, stale_reason = await get_psi_data()
    if data is None:
        await update.effective_chat.send_message(
            f"Could not fetch PSI data: {stale_reason}. Please try again later.",
            reply_markup=_keyboard(update),
        )
        return

    area, region = place if place else (None, None)
    token, chat_id, keyboard = context.bot.token, update.effective_chat.id, _keyboard(update)
    wants_map = await prefs.get_view(user_id) == prefs.VIEW_MAP
    png = await _render_map(data, area) if wants_map else None
    if png:
        # 1) map embedded in a rich message; 2) map as a plain photo + caption
        if await _send_rich(token, chat_id,
                            format_psi_rich(data, stale_reason, area, region, map_id=MAP_MEDIA_ID), png,
                            reply_markup=keyboard):
            return
        if await _send_map(token, chat_id, png, format_psi_caption(data, stale_reason, area, region),
                           reply_markup=keyboard):
            return
    if await _send_rich(token, chat_id, format_psi_rich(data, stale_reason, area, region),
                        reply_markup=keyboard):
        return

    text = (
        format_psi_message(data, stale_reason)
        if place is None
        else format_region_psi_message(data, *place, stale_reason)
    )
    await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


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


_awaiting_feedback: dict[str, float] = {}  # user -> deadline for their next message
_feedback_times: dict[str, list[float]] = {}  # user -> recent feedback timestamps


def _feedback_allowed(user_id: str, now: float) -> bool:
    """Sliding-window limit: RATE_LIMIT entries per RATE_WINDOW_SECS. Records the attempt if allowed."""
    for uid in list(_feedback_times):
        _feedback_times[uid] = [t for t in _feedback_times[uid] if now - t < feedback.RATE_WINDOW_SECS]
        if not _feedback_times[uid]:
            del _feedback_times[uid]
    times = _feedback_times.setdefault(user_id, [])
    if len(times) >= feedback.RATE_LIMIT:
        return False
    times.append(now)
    return True


async def _save_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    user = update.effective_user
    if not _feedback_allowed(str(user.id), time.monotonic()):
        await update.message.reply_text("You've sent a lot of feedback — please wait a minute.",
                                        reply_markup=_keyboard(update))
        return
    trimmed = len(text) > feedback.MAX_LEN
    try:
        entry = await feedback.save(user.id, user.username, text[:feedback.MAX_LEN])
    except Exception as exc:
        logger.warning("feedback save failed: %s", type(exc).__name__)
        await update.message.reply_text("Couldn't send that just now — please try again in a bit.",
                                        reply_markup=_keyboard(update))
        return
    try:
        await context.bot.send_message(ADMIN_USER_ID, f"💬 <b>New feedback</b>\n{feedback.format_entry(entry)}",
                                       parse_mode=ParseMode.HTML)
    except Exception as exc:  # it's saved either way; /feedbacks still lists it
        logger.warning("feedback DM to admin failed: %s", type(exc).__name__)
    note = f" It was trimmed to {feedback.MAX_LEN:,} characters." if trimmed else ""
    await update.message.reply_text(f"🙏 Thanks — your feedback was sent.{note}",
                                    reply_markup=_keyboard(update))


async def cmd_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Send me /feedback in a private chat.")
        return
    parts = update.message.text.split(maxsplit=1)  # keeps the feedback's own line breaks
    if len(parts) > 1:
        await _save_feedback(update, context, parts[1])
        return
    _awaiting_feedback[str(update.effective_user.id)] = time.monotonic() + feedback.AWAIT_SECS
    await update.message.reply_text("What would you like to tell me? Send it as your next message.",
                                    reply_markup=_keyboard(update))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain text in a private chat: feedback if /feedback asked for it, otherwise ignored."""
    deadline = _awaiting_feedback.pop(str(update.effective_user.id), None)
    if deadline is not None and time.monotonic() < deadline:
        await _save_feedback(update, context, update.message.text)


async def cmd_feedbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_USER_ID or update.effective_chat.type != ChatType.PRIVATE:
        return  # silent — indistinguishable from an unrecognized command
    n = 10
    if context.args and context.args[0].isdigit():
        n = max(1, min(int(context.args[0]), 50))
    entries = await feedback.recent(n)
    if not entries:
        await update.message.reply_text("No feedback yet.")
        return
    total = await feedback.count()
    blocks = [f"<b>Feedback</b> — newest {len(entries)} of {total}"]
    blocks += [feedback.format_entry(e) for e in entries]
    # Telegram caps a message at 4,096 characters; pack whole entries into each message.
    messages, current = [], ""
    for block in blocks:
        candidate = f"{current}\n\n———\n\n{block}" if current else block
        if len(candidate) > 4000 and current:
            messages.append(current)
            current = block
        else:
            current = candidate
    messages.append(current)
    for text in messages:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)


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
    # /stats and /feedbacks are deliberately absent so they never show in Telegram's command menu.
    commands = [
        BotCommand("psi", "Current PSI across Singapore"),
        BotCommand("location", "PSI for where you are"),
        BotCommand("alert", "Get notified when air quality changes"),
        BotCommand("view", "Choose map or text for /psi"),
        BotCommand("feedback", "Send feedback to the developer"),
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
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)  # handle users in parallel, not one at a time
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("psi", cmd_psi))
    # The keyboard's PSI button sends its label as plain text.
    app.add_handler(MessageHandler(
        filters.Text([PSI_BUTTON]) & filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE, cmd_psi))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("location", cmd_location))
    # UpdateType.MESSAGE excludes live-location edits, which arrive as
    # edited_message updates with update.message set to None.
    app.add_handler(MessageHandler(filters.LOCATION & filters.UpdateType.MESSAGE, on_location))
    app.add_handler(CommandHandler("alert", cmd_alert))
    app.add_handler(CallbackQueryHandler(on_alert_button, pattern=r"^alt:"))
    app.add_handler(CommandHandler("view", cmd_view))
    app.add_handler(CallbackQueryHandler(on_view_button, pattern=r"^view:"))
    app.add_handler(CommandHandler("feedback", cmd_feedback, filters=filters.UpdateType.MESSAGE))
    app.add_handler(CommandHandler("feedbacks", cmd_feedbacks, filters=filters.UpdateType.MESSAGE))
    # Must stay after the PSI-button handler: the first matching handler wins, so
    # tapping 🌫 Check PSI is never captured as feedback.
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE, on_text))
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
