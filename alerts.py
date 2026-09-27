import logging
import math
import time
from typing import Optional

from telegram.constants import ParseMode
from telegram.error import Forbidden
from telegram.ext import ContextTypes

from analytics import redis_client
from psi import PSI_BANDS, get_psi_data, psi_category, worst_region

logger = logging.getLogger(__name__)

ALERT_DURATIONS = (1, 3, 5, 7, 14)
USERS_KEY = "psi:alert_users"

CATEGORY_RANK = {label: i for i, (_, _, label, _) in enumerate(PSI_BANDS)}
BAD_CATEGORIES = {"Unhealthy", "Very Unhealthy", "Hazardous"}

# Verbatim from NEA's 24-hour PSI health advisory.
ADVICE = {
    "Good": {"all": "Normal activities for everyone."},
    "Moderate": {"all": "Normal activities for everyone."},
    "Unhealthy": {
        "healthy": "Reduce prolonged or strenuous outdoor physical exertion.",
        "vulnerable": "Minimise prolonged or strenuous outdoor physical exertion.",
        "chronic": "Avoid prolonged or strenuous outdoor physical exertion.",
    },
    "Very Unhealthy": {
        "healthy": "Avoid prolonged or strenuous outdoor physical exertion.",
        "vulnerable": "Minimise outdoor activity.",
        "chronic": "Avoid outdoor activity.",
    },
    "Hazardous": {
        "healthy": "Minimise outdoor activity.",
        "vulnerable": "Avoid outdoor activity.",
        "chronic": "Avoid outdoor activity.",
    },
}

GROUP_LABELS = {
    "healthy": "Healthy persons",
    "vulnerable": "Elderly, pregnant women &amp; children",
    "chronic": "Chronic lung or heart disease",
}

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


def advice_block(category: str) -> str:
    advice = ADVICE[category]
    if "all" in advice:
        return f"• {advice['all']}"
    return "\n".join(f"• <b>{GROUP_LABELS[g]}:</b> {advice[g]}" for g in GROUP_LABELS)


def format_alert(old: str, new: str, value: int, region: str, days_left: int) -> str:
    emoji = psi_category(value)[1]
    remaining = f"{days_left} more day{'s' if days_left != 1 else ''}"
    return (
        f"{emoji} <b>Air quality is now {new}</b>\n"
        f"<i>PSI {value} ({region.capitalize()}, highest of 5) — was {old}</i>\n"
        f"\n"
        f"{opening_line(old, new)}\n"
        f"\n"
        f"<b>NEA advisory</b>\n"
        f"{advice_block(new)}\n"
        f"\n"
        f"<i>Alerts on for {remaining} · /alert to change</i>"
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


async def get_subscription(user_id: str) -> Optional[dict]:
    sub = await redis_client.hgetall(_key(user_id))
    if not sub or float(sub["expires_at"]) <= time.time():
        return None
    return sub


async def subscribe(user_id: str, chat_id: int, days: int, category: str) -> None:
    pipe = redis_client.pipeline()
    pipe.hset(_key(user_id), mapping={
        "chat_id": chat_id,
        "expires_at": time.time() + days * 86400,
        "last_category": category,
    })
    pipe.sadd(USERS_KEY, user_id)
    await pipe.execute()


async def unsubscribe(user_id: str) -> None:
    pipe = redis_client.pipeline()
    pipe.delete(_key(user_id))
    pipe.srem(USERS_KEY, user_id)
    await pipe.execute()


def days_left(expires_at: float) -> int:
    return max(1, math.ceil((expires_at - time.time()) / 86400))


async def run_alert_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    user_ids = await redis_client.smembers(USERS_KEY)
    if not user_ids:
        return

    reading = None
    fetched = False
    now = time.time()

    for uid in user_ids:
        sub = await redis_client.hgetall(_key(uid))
        if not sub:
            await redis_client.srem(USERS_KEY, uid)
            continue
        chat_id = int(sub["chat_id"])
        expires_at = float(sub["expires_at"])
        try:
            if expires_at <= now:
                await unsubscribe(uid)
                await context.bot.send_message(
                    chat_id, "Your PSI alerts have ended. Send /alert to turn them back on."
                )
                continue

            if not fetched:
                reading = await current_reading()
                fetched = True
            if reading is None:
                continue  # fetch failed — retry next tick, but keep processing expiries

            category, value, region = reading
            old = sub["last_category"]
            if old == category:
                continue

            await context.bot.send_message(
                chat_id,
                format_alert(old, category, value, region, days_left(expires_at)),
                parse_mode=ParseMode.HTML,
            )
            await redis_client.hset(_key(uid), "last_category", category)
        except Forbidden:
            await unsubscribe(uid)  # user blocked the bot
        except Exception:
            logger.exception("Alert delivery failed")
