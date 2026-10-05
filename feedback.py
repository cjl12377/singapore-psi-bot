import html
import json
import time
from datetime import datetime
from typing import Optional

from analytics import SGT, redis_client

KEY = "psi:feedback"  # list, newest first
MAX_LEN = 1000  # characters kept per entry
MAX_STORED = 1000  # newest entries kept; older ones are trimmed
RATE_LIMIT = 5  # entries per user...
RATE_WINDOW_SECS = 60  # ...within this sliding window
AWAIT_SECS = 600  # how long a bare /feedback waits for the next message


async def save(user_id: int, username: Optional[str], text: str) -> dict:
    """Stores one entry (text, sender, time) and returns it."""
    entry = {"user_id": user_id, "username": username or "", "text": text, "ts": time.time()}
    pipe = redis_client.pipeline()
    pipe.lpush(KEY, json.dumps(entry, ensure_ascii=False))
    pipe.ltrim(KEY, 0, MAX_STORED - 1)
    await pipe.execute()
    return entry


async def recent(n: int) -> list[dict]:
    return [json.loads(raw) for raw in await redis_client.lrange(KEY, 0, n - 1)]


async def count() -> int:
    return await redis_client.llen(KEY)


def format_entry(entry: dict) -> str:
    """HTML (parse_mode=HTML). Everything the user supplied is escaped."""
    sender = f"id {entry['user_id']}"
    if entry.get("username"):
        sender = f"@{html.escape(entry['username'])} · {sender}"
    when = datetime.fromtimestamp(entry["ts"], SGT).strftime("%-d %b %Y, %-I:%M %p SGT")
    return f"From: {sender}\n<i>{when}</i>\n\n{html.escape(entry['text'])}"
