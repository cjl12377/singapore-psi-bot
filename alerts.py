import asyncio
import logging
import time
from typing import Optional

from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter
from telegram.ext import ContextTypes

from analytics import redis_client
from psi import PSI_BANDS, advice_block, get_psi_data, psi_category, worst_region

logger = logging.getLogger(__name__)

USERS_KEY = "psi:alert_users"
# PSI hovering on a band edge (e.g. 100 <-> 101) would otherwise alert every
# hour. Worsening always goes out; an improvement within this window of the
# last alert is held back until the reading has settled.
IMPROVEMENT_COOLDOWN_SECS = 3 * 3600
# Telegram allows about 30 messages/s across chats; pace the broadcast well under that.
SEND_INTERVAL_SECS = 0.05

CATEGORY_RANK = {label: i for i, (_, _, label, _) in enumerate(PSI_BANDS)}
BAD_CATEGORIES = {"Unhealthy", "Very Unhealthy", "Hazardous"}

ESCALATION_LINES = {
    "Unhealthy": "Uh oh, looks like it's not the best time to be outdoors.",
    "Very Unhealthy": "Heads up — air quality's taken a real dip. Keep outdoor time short today if you can.",
    "Hazardous": "This one's serious — the air's hazardous right now. Best to stay indoors if you can.",
}
IMPROVING_STILL_BAD_LINE = "Easing up a little, but still {category} — I'd hold off on anything strenuous outdoors."
ALL_CLEAR_LINE = "Good news — the air's cleared up. You're good to head back outside."
MILD_CHANGE_LINE = "Small shift in air quality — nothing to worry about yet."


def _key(user_id: str) -> str:
    return f"psi:alert:{user_id}"


def opening_line(old: str, new: str) -> Optional[str]:
    """Casual lead-in for a category change, or None if nothing changed."""
    if old == new or old not in CATEGORY_RANK or new not in CATEGORY_RANK:
        return None
    worse = CATEGORY_RANK[new] > CATEGORY_RANK[old]
    if new in BAD_CATEGORIES:
        return ESCALATION_LINES[new] if worse else IMPROVING_STILL_BAD_LINE.format(category=new)
    if old in BAD_CATEGORIES:
        return ALL_CLEAR_LINE
    return MILD_CHANGE_LINE  # Good <-> Moderate


def format_alert(old: str, new: str, value: int, region: str) -> str:
    emoji = psi_category(value)[1]
    return (
        f"{emoji} <b>Air quality is now {new}</b>\n"
        f"<i>PSI {value} ({region.capitalize()}, highest of 5) — was {old}</i>\n"
        f"\n"
        f"{opening_line(old, new)}\n"
        f"\n"
        f"<b>PSI Health Warnings as per NEA</b>\n"
        f"{advice_block(new)}\n"
        f"\n"
        f"<i>/alert to turn these off</i>"
    )


async def current_reading() -> Optional[tuple[str, int, str]]:
    """(category, value, region) from fresh data only — never alert on stale data."""
    data, stale_reason = await get_psi_data()
    if data is None or stale_reason:
        return None
    try:
        region, value = worst_region(data)
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return psi_category(value)[0], value, region


async def is_subscribed(user_id: str) -> bool:
    return bool(await redis_client.exists(_key(user_id)))


async def subscribe(user_id: str, chat_id: int, category: str) -> None:
    pipe = redis_client.pipeline()
    pipe.delete(_key(user_id))
    pipe.hset(_key(user_id), mapping={"chat_id": chat_id, "last_category": category})
    pipe.sadd(USERS_KEY, user_id)
    await pipe.execute()


async def unsubscribe(user_id: str) -> None:
    pipe = redis_client.pipeline()
    pipe.delete(_key(user_id))
    pipe.srem(USERS_KEY, user_id)
    await pipe.execute()


def should_alert(old: str, new: str, last_alert_at: float, now: float) -> bool:
    if old == new:
        return False
    worse = CATEGORY_RANK.get(new, 0) > CATEGORY_RANK.get(old, 0)
    return worse or now - last_alert_at >= IMPROVEMENT_COOLDOWN_SECS


async def run_alert_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    user_ids = await redis_client.smembers(USERS_KEY)
    if not user_ids:
        return

    reading = await current_reading()
    if reading is None:
        return  # fetch failed or data stale — try again next tick
    category, value, region = reading
    now = time.time()

    for uid in user_ids:
        sub = await redis_client.hgetall(_key(uid))
        if not sub:
            await redis_client.srem(USERS_KEY, uid)
            continue
        old = sub["last_category"]
        if not should_alert(old, category, float(sub.get("last_alert_at", 0)), now):
            continue
        try:
            await _send_alert(context.bot, int(sub["chat_id"]), format_alert(old, category, value, region))
            await redis_client.hset(_key(uid), mapping={"last_category": category, "last_alert_at": now})
        except Forbidden:
            await unsubscribe(uid)  # user blocked the bot
        except Exception:
            logger.exception("Alert delivery failed")  # not recorded, so retried next check
        await asyncio.sleep(SEND_INTERVAL_SECS)


async def _send_alert(bot, chat_id: int, text: str) -> None:
    """One send; if Telegram says to slow down, wait as told and try once more."""
    try:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    except RetryAfter as exc:
        await asyncio.sleep(exc.retry_after)
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
