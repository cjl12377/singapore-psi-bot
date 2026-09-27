import logging
import os
import time
from datetime import datetime, timedelta, timezone

import redis.asyncio as redis

logging.getLogger("redis").setLevel(logging.WARNING)

REDIS_URL = os.environ["REDIS_URL"]
redis_client = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=10)

SGT = timezone(timedelta(hours=8))
RETENTION_GAP_SECS = 12 * 3600
RETENTION_MIN_SESSIONS = 4  # >3 requests each >12h apart
MAX_HISTORY_PER_USER = 100


def _today_str() -> str:
    return datetime.now(SGT).strftime("%Y-%m-%d")


async def track_request(user_id: str) -> None:
    """Best-effort usage tracking — never raises, so analytics can't break /psi."""
    try:
        now = time.time()
        today = _today_str()

        pipe = redis_client.pipeline()
        pipe.sadd("psi:all_users", user_id)
        pipe.zadd("psi:last_seen", {user_id: now})
        pipe.zadd(f"psi:requests:{user_id}", {str(now): now})
        pipe.zremrangebyrank(f"psi:requests:{user_id}", 0, -(MAX_HISTORY_PER_USER + 1))
        results = await pipe.execute()

        is_new_user = results[0] == 1
        if is_new_user:
            await redis_client.sadd(f"psi:new_users:{today}", user_id)
    except Exception:
        logging.getLogger(__name__).exception("Analytics tracking failed")


async def active_users_24h() -> int:
    cutoff = time.time() - 86400
    return await redis_client.zcount("psi:last_seen", cutoff, "+inf")


async def total_unique_users() -> int:
    return await redis_client.scard("psi:all_users")


async def daily_growth(days: int = 7) -> list[tuple[str, int]]:
    today = datetime.now(SGT).date()
    counts = []
    for i in range(days):
        d = today - timedelta(days=i)
        count = await redis_client.scard(f"psi:new_users:{d.isoformat()}")
        counts.append((d.isoformat(), count))
    return list(reversed(counts))


async def retained_users() -> int:
    """Users with 4+ sessions, each session >12h after the previous one."""
    all_users = await redis_client.smembers("psi:all_users")
    retained = 0
    for uid in all_users:
        entries = await redis_client.zrange(f"psi:requests:{uid}", 0, -1, withscores=True)
        if not entries:
            continue
        timestamps = sorted(score for _, score in entries)
        sessions = 1
        last_session_ts = timestamps[0]
        for ts in timestamps[1:]:
            if ts - last_session_ts > RETENTION_GAP_SECS:
                sessions += 1
                last_session_ts = ts
        if sessions >= RETENTION_MIN_SESSIONS:
            retained += 1
    return retained
