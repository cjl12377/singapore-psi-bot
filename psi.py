import asyncio
import html
import time
from datetime import datetime
from typing import Optional

import httpx

PSI_API_URL = "https://api-open.data.gov.sg/v2/real-time/api/psi"
PM25_API_URL = "https://api-open.data.gov.sg/v2/real-time/api/pm25"
CACHE_TTL = 600  # 10 minutes — API updates hourly, so this is conservative

# After a failed fetch, serve the cache (or the error) without retrying for this long,
# so an outage doesn't make every request wait out the timeout.
RETRY_AFTER_FAILURE = 60

_cache: dict = {"data": None, "timestamp": 0.0, "stale_reason": None, "failed_at": 0.0}
# One fetch at a time: when the cache expires under load, the first request refreshes
# it and the rest reuse the result (data.gov.sg allows 6 calls / 10 s without a key).
_fetch_lock = asyncio.Lock()

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


async def _attach_pm25(client: httpx.AsyncClient, data: dict) -> None:
    """The PSI endpoint has no 1-hour PM2.5; it lives on its own endpoint. Merge it into
    the PSI reading as readings["pm25_one_hourly"]. Best effort: absent -> PSI headline."""
    try:
        resp = await client.get(PM25_API_URL)
        resp.raise_for_status()
        pm25 = resp.json()["data"]["items"][0]["readings"]["pm25_one_hourly"]
        data["data"]["items"][0]["readings"]["pm25_one_hourly"] = pm25
    except Exception:
        pass


async def get_psi_data() -> tuple[Optional[dict], Optional[str]]:
    """Returns (data, stale_reason). stale_reason is None when data is fresh; when
    data is None, stale_reason says why the fetch failed."""
    async with _fetch_lock:
        now = time.time()
        if _cache["data"] and now - _cache["timestamp"] < CACHE_TTL:
            return _cache["data"], None
        if now - _cache["failed_at"] < RETRY_AFTER_FAILURE:
            return _cache["data"], _cache["stale_reason"]

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(PSI_API_URL)
                resp.raise_for_status()
                data = resp.json()
                await _attach_pm25(client, data)
                _cache.update(data=data, timestamp=now, stale_reason=None, failed_at=0.0)
                return data, None
        except Exception as exc:
            reason = _classify_error(exc)
            _cache.update(stale_reason=reason, failed_at=now)
            return _cache["data"], reason


REGIONS = ["central", "north", "south", "east", "west"]

LEGEND_ROWS = [
    ("🟢", "Good", "0-50"),
    ("🟡", "Moderate", "51-100"),
    ("🟠", "Unhealthy", "101-200"),
    ("🔴", "V. Unhealthy", "201-300"),
    ("🟣", "Hazardous", ">300"),
]


# Verbatim from NEA's 24-hour PSI health advisory — do not reword.
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
    "vulnerable": "Elderly, pregnant women & children",
    "chronic": "Chronic lung or heart disease",
}


def advice_block(category: str) -> str:
    """HTML (parse_mode=HTML) bullets."""
    advice = ADVICE[category]
    if "all" in advice:
        return f"• {advice['all']}"
    return "\n".join(f"• <b>{html.escape(GROUP_LABELS[g])}:</b> {advice[g]}" for g in GROUP_LABELS)


def advice_markdown(category: str) -> str:
    """Rich-message Markdown list."""
    advice = ADVICE[category]
    if "all" in advice:
        return f"- {advice['all']}"
    return "\n".join(f"- **{GROUP_LABELS[g]}:** {advice[g]}" for g in GROUP_LABELS)


def worst_region(data: dict) -> tuple[str, int]:
    """v2 API reports only regional values, no national aggregate —
    the worst (highest) region stands in as the headline figure."""
    psi = data["data"]["items"][0]["readings"]["psi_twenty_four_hourly"]
    return max(psi.items(), key=lambda kv: kv[1])


def headline(data: dict, region: Optional[str]) -> tuple[str, int, Optional[int]]:
    """(region, 24-hr PSI, 1-hr PM2.5 or None). Headline region: the one picked, else the
    highest 1-hr PM2.5 (falling back to highest PSI if PM2.5 is unavailable)."""
    readings = data["data"]["items"][0]["readings"]
    psi, pm25 = readings["psi_twenty_four_hourly"], readings.get("pm25_one_hourly")
    if region is None:
        region = max((pm25 or psi).items(), key=lambda kv: kv[1])[0]
    return region, psi[region], (pm25[region] if pm25 else None)


def format_psi_message(data: dict, stale_reason: Optional[str] = None) -> str:
    return _format(data, stale_reason, region=None, area=None)


def format_region_psi_message(
    data: dict, area: str, region: str, stale_reason: Optional[str] = None
) -> str:
    return _format(data, stale_reason, region=region, area=area)


def _format(data: dict, stale_reason: Optional[str], region: Optional[str], area: Optional[str]) -> str:
    try:
        items = data["data"]["items"]
        if not items:
            return "No PSI readings are currently available."

        latest = items[0]
        updated = _fmt_timestamp(latest.get("updatedTimestamp", ""))
        psi = latest["readings"]["psi_twenty_four_hourly"]

        headline_region, headline_psi, headline_pm25 = headline(data, region)
        pm25 = latest["readings"].get("pm25_one_hourly")
        if region is None:
            subtitle = f"{headline_region.capitalize()} region, highest of 5"
        else:
            subtitle = f"📍 {area} · {region.capitalize()} region"
        category, emoji = psi_category(headline_psi)
        if headline_pm25 is not None:
            title = (f"{emoji} <b>PM2.5 {headline_pm25} µg/m³</b> (1-hr)\n"
                     f"<b>PSI {headline_psi} — {category}</b> (24-hr)")
        else:
            title = f"{emoji} <b>PSI {headline_psi} — {category}</b>"

        region_lines = "\n".join(
            f"{psi_category(psi[r])[1]} {r.capitalize()} — "
            + (f"PM2.5 {pm25[r]} · " if pm25 else "")
            + f"PSI {psi[r]}"
            for r in REGIONS
        )

        legend_header = f"{'':<16}{'PSI Range':>9}"
        legend_rows = "\n".join(
            f"{e} {label:<13}{rng:>9}" for e, label, rng in LEGEND_ROWS
        )

        stale_banner = (
            f"⚠️ <b>Stale data</b> — live fetch failed: {stale_reason}\n\n"
            if stale_reason else ""
        )

        return (
            f"{stale_banner}"
            f"🕐 <i>Last updated: {updated}</i>\n"
            f"\n"
            f"{title}\n"
            f"<i>{subtitle}</i>\n"
            f"\n"
            f"<b>Regional Breakdown</b>\n"
            f"{region_lines}\n"
            f"\n"
            f"<b>PSI Health Warnings as per NEA</b>\n{advice_block(category)}\n"
            f"\n"
            f"<blockquote><pre>{legend_header}\n{legend_rows}</pre></blockquote>"
        )
    except (KeyError, IndexError, TypeError, ValueError):
        return "Error parsing PSI data. The API response format may have changed."


def format_psi_caption(
    data: dict,
    stale_reason: Optional[str] = None,
    area: Optional[str] = None,
    region: Optional[str] = None,
) -> str:
    """HTML caption for the PSI map photo (headline only; the map carries the breakdown)."""
    try:
        latest = data["data"]["items"][0]
        updated = _fmt_timestamp(latest.get("updatedTimestamp", ""))
        psi = latest["readings"]["psi_twenty_four_hourly"]
        headline_region, headline_psi, headline_pm25 = headline(data, region)
        if region is None:
            subtitle = f"{headline_region.capitalize()} region · highest of 5"
        else:
            subtitle = f"📍 {area} · {region.capitalize()} region"
        category, emoji = psi_category(headline_psi)
        if headline_pm25 is not None:
            title = (f"{emoji} <b>PM2.5 {headline_pm25} µg/m³</b> (1-hr)\n"
                     f"<b>PSI {headline_psi} — {category}</b> (24-hr)")
        else:
            title = f"{emoji} <b>PSI {headline_psi} — {category}</b>"
        banner = (
            f"⚠️ <b>Stale data</b> — live fetch failed: {stale_reason}\n\n"
            if stale_reason else ""
        )
        return (
            f"{banner}{title}\n"
            f"{subtitle}\n"
            f"<i>🕐 Updated {updated}</i>\n\n"
            f"<b>PSI Health Warnings as per NEA</b>\n{advice_block(category)}"
        )
    except (KeyError, IndexError, TypeError, ValueError):
        return "Error parsing PSI data. The API response format may have changed."


def format_psi_rich(
    data: dict,
    stale_reason: Optional[str] = None,
    area: Optional[str] = None,
    region: Optional[str] = None,
    map_id: Optional[str] = None,
) -> str:
    """Rich-tier (sendRichMessage) Markdown. See .claude/skills/psi-rich-format.

    With map_id, the regional table and guide are replaced by the uploaded map image
    (referenced as tg://photo?id={map_id}; the caller supplies it in `media`)."""
    try:
        latest = data["data"]["items"][0]
        updated = _fmt_timestamp(latest.get("updatedTimestamp", ""))
        psi = latest["readings"]["psi_twenty_four_hourly"]

        headline_region, headline_psi, headline_pm25 = headline(data, region)
        pm25 = latest["readings"].get("pm25_one_hourly")
        if region is None:
            subtitle = f"{headline_region.capitalize()} region · highest of 5"
        else:
            subtitle = f"📍 {area} · {region.capitalize()} region"
        category, emoji = psi_category(headline_psi)

        rows = "\n".join(
            f"| {psi_category(psi[r])[1]} {r.capitalize()}"
            f"{' ◀' if r == headline_region else ''} "
            + (f"| **{pm25[r]}** " if pm25 else "")
            + f"| {psi[r]} | {psi_category(psi[r])[0]} |"
            for r in REGIONS
        )
        table_head = (
            "| Region | PM2.5 (1-hr) | PSI (24-hr) | Level |\n|:--|--:|--:|:--|"
            if pm25 else "| Region | PSI (24-hr) | Level |\n|:--|--:|:--|"
        )
        legend = "\n".join(f"| {e} {label} | {rng} |" for e, label, rng in LEGEND_ROWS)
        banner = (
            f"> ⚠️ **Stale data** — live fetch failed: {stale_reason}\n\n"
            if stale_reason else ""
        )

        if headline_pm25 is not None:
            title = f"# {emoji} PM2.5 {headline_pm25} µg/m³"
            line2 = f"**1-hour reading** · {subtitle}\n**PSI {headline_psi} · {category}** (24-hr)"
        else:
            title = f"# {emoji} PSI {headline_psi}"
            line2 = f"**{category}** · {subtitle}"
        head = (
            f"{banner}"
            f"{title}\n"
            f"{line2}\n"
            f"*🕐 Updated {updated}*\n\n"
        )
        advisory = f"### PSI Health Warnings as per NEA\n\n{advice_markdown(category)}\n\n"
        if map_id:
            return f"{head}![PSI by region](tg://photo?id={map_id})\n\n{advisory.rstrip()}"

        return (
            f"{head}"
            f"---\n\n"
            f"### Regional breakdown\n\n"
            f"{table_head}\n"
            f"{rows}\n\n"
            f"{advisory}"
            f"### PSI Categories\n\n"
            f"| Level | PSI range |\n"
            f"|:--|--:|\n"
            f"{legend}"
        )
    except (KeyError, IndexError, TypeError, ValueError):
        return "Error parsing PSI data. The API response format may have changed."
