import logging
import math
import os
import re
import time
from datetime import date, datetime, timedelta, timezone

import redis.asyncio as redis

logging.getLogger("redis").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

REDIS_URL = os.environ["REDIS_URL"]
redis_client = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=10)

SGT = timezone(timedelta(hours=8))
RETENTION_GAP_SECS = 12 * 3600
RETENTION_MIN_SESSIONS = 4  # >3 requests each >12h apart
MAX_HISTORY_PER_USER = 100

# Per-day keys (active sets, event counts, reply times, unknown commands) expire after
# this long: enough for 14 cohorts plus a 30-day return window, with room to spare.
DAILY_TTL_SECS = 100 * 86400
MAX_TIMINGS_PER_DAY = 1000
MAX_ALERT_LAGS = 200
MAX_UNKNOWN_COMMAND_NAMES = 100  # per day, so junk commands can't grow a key without bound
FOLLOWUP_WINDOW_SECS = 3600  # a /psi this soon after an alert counts as the alert being read
COHORT_DAYS = 14
RETURN_WINDOWS = (1, 7, 30)

ACTIVE_SINCE_KEY = "psi:active_since"
ALERT_LAG_KEY = "psi:alert_lag"
GROUPS_KEY = "psi:groups"
ALERT_USERS_KEY = "psi:alert_users"  # owned by alerts.py; read here for /stats


def _today() -> date:
    return datetime.now(SGT).date()


def _today_str() -> str:
    return _today().isoformat()


def _active_key(day: str) -> str:
    return f"psi:active:{day}"


def _events_key(day: str) -> str:
    return f"psi:events:{day}"


def _reply_ms_key(day: str) -> str:
    return f"psi:reply_ms:{day}"


def _e2e_ms_key(day: str) -> str:
    return f"psi:e2e_ms:{day}"


def _unknown_key(day: str) -> str:
    return f"psi:unknown_cmds:{day}"


def _alerted_key(user_id: str) -> str:
    return f"psi:alerted:{user_id}"


def _count_events(pipe, day: str, events) -> None:
    for name in events:
        pipe.hincrby(_events_key(day), name, 1)
    pipe.expire(_events_key(day), DAILY_TTL_SECS)


def _push_timing(pipe, key: str, ms: float) -> None:
    pipe.lpush(key, int(ms))
    pipe.ltrim(key, 0, MAX_TIMINGS_PER_DAY - 1)
    pipe.expire(key, DAILY_TTL_SECS)


async def track_request(user_id: str, events=(), reply_ms: float | None = None,
                        e2e_ms: float | None = None) -> None:
    """Record one served PSI request. Best-effort — never raises, so analytics can't break /psi.

    events: names counted in today's event tally (e.g. "psi", "sent_photo").
    reply_ms: time spent in the handler; e2e_ms: from the user's message to the reply."""
    try:
        now = time.time()
        today = _today_str()

        pipe = redis_client.pipeline()
        pipe.sadd("psi:all_users", user_id)
        pipe.zadd("psi:last_seen", {user_id: now})
        pipe.zadd(f"psi:requests:{user_id}", {str(now): now})
        pipe.zremrangebyrank(f"psi:requests:{user_id}", 0, -(MAX_HISTORY_PER_USER + 1))
        pipe.get(_alerted_key(user_id))
        pipe.delete(_alerted_key(user_id))
        pipe.sadd(_active_key(today), user_id)
        pipe.expire(_active_key(today), DAILY_TTL_SECS)
        pipe.set(ACTIVE_SINCE_KEY, today, nx=True)
        _count_events(pipe, today, events)
        if reply_ms is not None:
            _push_timing(pipe, _reply_ms_key(today), reply_ms)
        if e2e_ms is not None:
            _push_timing(pipe, _e2e_ms_key(today), e2e_ms)
        results = await pipe.execute()

        is_new_user, followed_alert = results[0] == 1, results[4] is not None
        if is_new_user or followed_alert:
            pipe = redis_client.pipeline()
            if is_new_user:
                pipe.sadd(f"psi:new_users:{today}", user_id)
            if followed_alert:
                _count_events(pipe, today, ["alert_followup"])
            await pipe.execute()
    except Exception:
        logger.exception("Analytics tracking failed")


async def count(*events: str) -> None:
    """Add one to each named event in today's tally. Never raises."""
    try:
        pipe = redis_client.pipeline()
        _count_events(pipe, _today_str(), events)
        await pipe.execute()
    except Exception:
        logger.exception("Analytics event count failed")


async def record_alert_check(sent_to: list[str], failed: int, blocked: int, held: int,
                             lag_secs: float | None) -> None:
    """Tally one alert run. sent_to: users alerted (marked for follow-up tracking);
    lag_secs: from the reading's publish time to the first worsening alert. Never raises."""
    try:
        today = _today_str()
        pipe = redis_client.pipeline()
        events = (["alert_sent"] * len(sent_to) + ["alert_failed"] * failed
                  + ["alert_blocked"] * blocked + ["alert_held"] * held)
        _count_events(pipe, today, events)
        for uid in sent_to:
            pipe.set(_alerted_key(uid), "1", ex=FOLLOWUP_WINDOW_SECS)
        if lag_secs is not None:
            pipe.lpush(ALERT_LAG_KEY, int(lag_secs))
            pipe.ltrim(ALERT_LAG_KEY, 0, MAX_ALERT_LAGS - 1)
        await pipe.execute()
    except Exception:
        logger.exception("Alert analytics failed")


_COMMAND_RE = re.compile(r"^/([A-Za-z0-9_]{1,32})(?:@([A-Za-z0-9_]+))?$")


def parse_command(text: str, bot_username: str | None) -> str | None:
    """The lowercased name of a command message's command, or None if it isn't a
    well-formed command or is addressed to a different bot (/cmd@OtherBot)."""
    words = (text or "").split(maxsplit=1)
    match = _COMMAND_RE.match(words[0]) if words else None
    if not match:
        return None
    name, target = match.groups()
    if target and (not bot_username or target.lower() != bot_username.lower()):
        return None
    return name.lower()


async def record_unknown_command(name: str) -> None:
    """Count a command the bot doesn't handle, by name. Never raises."""
    try:
        today = _today_str()
        key = _unknown_key(today)
        known = await redis_client.hexists(key, name)
        pipe = redis_client.pipeline()
        _count_events(pipe, today, ["unknown_cmd"])
        if known or await redis_client.hlen(key) < MAX_UNKNOWN_COMMAND_NAMES:
            pipe.hincrby(key, name, 1)
            pipe.expire(key, DAILY_TTL_SECS)
        await pipe.execute()
    except Exception:
        logger.exception("Unknown-command analytics failed")


_PRESENT = {"member", "administrator", "creator", "restricted"}


def chat_member_event(chat_type: str, old_status: str, new_status: str) -> str | None:
    """Classify a change to the bot's own membership of a chat:
    private chats -> "blocked"/"unblocked"; groups -> "group_add"/"group_remove"."""
    if chat_type == "private":
        if new_status == "kicked" and old_status != "kicked":
            return "blocked"
        if old_status == "kicked" and new_status != "kicked":
            return "unblocked"
    elif chat_type in ("group", "supergroup"):
        was, now = old_status in _PRESENT, new_status in _PRESENT
        if now and not was:
            return "group_add"
        if was and not now:
            return "group_remove"
    return None


async def record_chat_member(event: str, chat_id: int) -> None:
    """Tally a block/unblock or group add/remove, and keep the group set current. Never raises."""
    try:
        pipe = redis_client.pipeline()
        _count_events(pipe, _today_str(), [event])
        if event == "group_add":
            pipe.sadd(GROUPS_KEY, chat_id)
        elif event == "group_remove":
            pipe.srem(GROUPS_KEY, chat_id)
        await pipe.execute()
    except Exception:
        logger.exception("Chat member analytics failed")


async def active_users_24h() -> int:
    cutoff = time.time() - 86400
    return await redis_client.zcount("psi:last_seen", cutoff, "+inf")


async def total_unique_users() -> int:
    return await redis_client.scard("psi:all_users")


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


# --- /stats report: pure helpers, then one fetch ---------------------------------------

def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile (p in 0-100), or None for no data."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def cohort_return(new_by_day: dict[date, set], active_by_day: dict[date, set], window: int,
                  today: date, since: date | None, cohort_days: int = COHORT_DAYS) -> tuple[int, int]:
    """(returned, cohort size) over the latest `cohort_days` complete cohorts: users new on
    day d who were active again on any of days d+1 … d+window.

    A cohort counts only if its whole window has passed (d + window <= yesterday) and
    activity was being recorded throughout (d >= since) — otherwise users would look like
    they never came back when the data just doesn't exist."""
    if since is None:
        return 0, 0
    returned = size = 0
    last_cohort = today - timedelta(days=window + 1)
    for i in range(cohort_days):
        d = last_cohort - timedelta(days=i)
        if d < since:
            break
        cohort = new_by_day.get(d, set())
        later = set().union(*(active_by_day.get(d + timedelta(days=k), set())
                              for k in range(1, window + 1)))
        returned += len(cohort & later)
        size += len(cohort)
    return returned, size


def _sum_counts(hashes: list[dict]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for h in hashes:
        for name, n in (h or {}).items():
            totals[name] = totals.get(name, 0) + int(n)
    return totals


async def report(days: int = 7) -> dict:
    """Everything /stats shows, fetched in one round trip (plus the all-time regulars scan)."""
    today = _today()
    span = COHORT_DAYS + max(RETURN_WINDOWS) + 1  # far enough back for every cohort window
    history = [today - timedelta(days=i) for i in range(span)]
    recent = history[:days]

    pipe = redis_client.pipeline()
    pipe.get(ACTIVE_SINCE_KEY)
    pipe.scard(ALERT_USERS_KEY)
    pipe.scard(GROUPS_KEY)
    pipe.lrange(ALERT_LAG_KEY, 0, -1)
    for d in history:
        pipe.smembers(_active_key(d.isoformat()))
    for d in history:
        pipe.smembers(f"psi:new_users:{d.isoformat()}")
    for d in recent:
        pipe.hgetall(_events_key(d.isoformat()))
    for d in recent:
        pipe.hgetall(_unknown_key(d.isoformat()))
    for d in recent:
        pipe.lrange(_reply_ms_key(d.isoformat()), 0, -1)
    for d in recent:
        pipe.lrange(_e2e_ms_key(d.isoformat()), 0, -1)
    res = await pipe.execute()

    since_str, alert_subs, groups, lags = res[:4]
    pos = 4
    active = dict(zip(history, res[pos:pos + span])); pos += span
    new = dict(zip(history, res[pos:pos + span])); pos += span
    events = _sum_counts(res[pos:pos + days]); pos += days
    unknown = _sum_counts(res[pos:pos + days]); pos += days
    reply_ms = [int(v) for lst in res[pos:pos + days] for v in lst]; pos += days
    e2e_ms = [int(v) for lst in res[pos:pos + days] for v in lst]
    since = date.fromisoformat(since_str) if since_str else None

    def union_size(n: int) -> int:
        return len(set().union(*(active[d] for d in history[:n])))

    # Average daily actives over the last 30 complete days that have data.
    full_days = [d for d in history[1:31] if since and d >= since]
    avg_dau = sum(len(active[d]) for d in full_days) / len(full_days) if full_days else None

    active_today, new_today = active[today], new[today] & active[today]
    total_users = await total_unique_users()
    return {
        "since": since,
        "active_24h": await active_users_24h(),
        "total_users": total_users,
        "regulars": await retained_users(),
        "today_active": len(active_today),
        "today_new": len(new_today),
        "wau": union_size(7),
        "mau": union_size(30),
        "avg_dau": avg_dau,
        "alert_subs": alert_subs,
        "groups": groups,
        "returns": {w: cohort_return(new, active, w, today, since) for w in RETURN_WINDOWS},
        "daily": [(d.isoformat(), len(new[d]), len(active[d])) for d in reversed(recent)],
        "events": events,
        "unknown": sorted(unknown.items(), key=lambda kv: (-kv[1], kv[0])),
        "reply_ms": reply_ms,
        "e2e_ms": e2e_ms,
        "alert_lags": [int(v) for v in lags],
    }
