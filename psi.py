import time
from datetime import datetime
from typing import Optional

import httpx

PSI_API_URL = "https://api-open.data.gov.sg/v2/real-time/api/psi"
CACHE_TTL = 600  # 10 minutes — API updates hourly, so this is conservative

_cache: dict = {"data": None, "timestamp": 0.0, "stale_reason": None}

PSI_BANDS = [
    (0, 50, "Good", "🟢"),
    (51, 100, "Moderate", "🟡"),
    (101, 200, "Unhealthy", "🟠"),
    (201, 300, "Very Unhealthy", "🔴"),
    (301, float("inf"), "Hazardous", "🟣"),
]


def psi_category(value: int | float) -> tuple[str, str]:
    """Returns (label, emoji)."""
    for lo, hi, label, emoji in PSI_BANDS:
        if lo <= value <= hi:
            return label, emoji
    return "Unknown", "⚪"


def _fmt_timestamp(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%-d %b %Y, %-I:%M %p SGT")
    except Exception:
        return ts


def _classify_error(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "request to data.gov.sg timed out"
    if isinstance(exc, httpx.ConnectError):
        return "could not connect to data.gov.sg"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"data.gov.sg returned HTTP {exc.response.status_code}"
    return f"unexpected error: {type(exc).__name__}"


async def get_psi_data() -> tuple[Optional[dict], Optional[str]]:
    """Returns (data, stale_reason). stale_reason is None when data is fresh."""
    now = time.time()
    if _cache["data"] and now - _cache["timestamp"] < CACHE_TTL:
        return _cache["data"], None

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(PSI_API_URL)
            resp.raise_for_status()
            data = resp.json()
            _cache["data"] = data
            _cache["timestamp"] = now
            _cache["stale_reason"] = None
            return data, None
    except Exception as exc:
        reason = _classify_error(exc)
        _cache["stale_reason"] = reason
        return _cache["data"], reason if _cache["data"] else None


REGIONS = ["north", "south", "east", "west", "central"]


def format_psi_message(data: dict, stale_reason: Optional[str] = None) -> str:
    try:
        items = data["data"]["items"]
        if not items:
            return "No PSI readings are currently available."

        latest = items[0]
        timestamp = _fmt_timestamp(latest.get("timestamp", ""))
        readings = latest["readings"]

        psi = readings.get("psi_twenty_four_hourly", {})
        pm25 = readings.get("pm25_twenty_four_hourly", {})

        # v2 API reports only regional values, no national aggregate —
        # use the worst (highest) region as the headline figure.
        worst_region, worst_psi = max(psi.items(), key=lambda kv: kv[1])
        category, emoji = psi_category(worst_psi)

        header = f"{'Region':<8}{'PSI':>4} {'PM2.5':>5}"
        region_lines = "\n".join(
            f"{region.capitalize():<8}{psi.get(region, 0):>4} {pm25.get(region, 0):>5}"
            for region in REGIONS
        )

        stale_banner = (
            f"⚠️ <b>Stale data</b> — live fetch failed: {stale_reason}\n\n"
            if stale_reason else ""
        )

        return (
            f"{stale_banner}"
            f"{emoji} <b>PSI {worst_psi} — {category}</b>\n"
            f"<i>{worst_region.capitalize()}, highest of 5 regions</i>\n"
            f"\n"
            f"<b>Singapore Air Quality</b> · {timestamp}\n"
            f"\n"
            f"<pre>{header}\n{region_lines}</pre>\n"
            f"🟢 0-50 Good | 🟡 51-100 Moderate | 🟠 101-200 Unhealthy\n"
            f"🔴 201-300 Very Unhealthy | 🟣 301+ Hazardous"
        )
    except (KeyError, IndexError, TypeError, ValueError):
        return "Error parsing PSI data. The API response format may have changed."
