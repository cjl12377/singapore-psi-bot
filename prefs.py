import logging

from analytics import redis_client

logger = logging.getLogger(__name__)

VIEW_MAP, VIEW_TEXT = "map", "text"
DEFAULT_VIEW = VIEW_MAP


def _key(user_id: str) -> str:
    return f"psi:pref:{user_id}"


async def get_view(user_id: str) -> str:
    """'map' or 'text'. Falls back to the default if Redis is unreachable."""
    try:
        value = await redis_client.hget(_key(user_id), "view")
    except Exception as exc:
        logger.warning("view preference read failed: %s", type(exc).__name__)
        return DEFAULT_VIEW
    return value if value in (VIEW_MAP, VIEW_TEXT) else DEFAULT_VIEW


async def set_view(user_id: str, view: str) -> None:
    await redis_client.hset(_key(user_id), "view", view)
