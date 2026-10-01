import html
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

        if region is None:
            headline_region, headline_psi = worst_region(data)
            subtitle = f"{headline_region.capitalize()} region, highest of 5"
        else:
            headline_region, headline_psi = region, psi[region]
            subtitle = f"📍 {area} · {region.capitalize()} region"
        category, emoji = psi_category(headline_psi)

        region_lines = "\n".join(
            f"{psi_category(psi[region])[1]} {region.capitalize()} — {psi[region]}"
            for region in REGIONS
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
            f"{emoji} <b>PSI {headline_psi} — {category}</b>\n"
            f"<i>{subtitle}</i>\n"
            f"\n"
            f"<b>Regional Breakdown</b>\n"
            f"{region_lines}\n"
            f"\n"
            f"<b>NEA advisory</b>\n{advice_block(category)}\n"
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
        if region is None:
            headline_region, headline_psi = worst_region(data)
            subtitle = f"{headline_region.capitalize()} region · highest of 5"
        else:
            headline_psi = psi[region]
            subtitle = f"📍 {area} · {region.capitalize()} region"
        category, emoji = psi_category(headline_psi)
        banner = (
            f"⚠️ <b>Stale data</b> — live fetch failed: {stale_reason}\n\n"
            if stale_reason else ""
        )
        return (
            f"{banner}{emoji} <b>PSI {headline_psi} — {category}</b>\n"
            f"{subtitle}\n"
            f"<i>🕐 Updated {updated}</i>\n\n"
            f"<b>NEA advisory</b>\n{advice_block(category)}"
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

        if region is None:
            headline_region, headline_psi = worst_region(data)
            subtitle = f"{headline_region.capitalize()} region · highest of 5"
        else:
            headline_region, headline_psi = region, psi[region]
            subtitle = f"📍 {area} · {region.capitalize()} region"
        category, emoji = psi_category(headline_psi)

        rows = "\n".join(
            f"| {psi_category(psi[r])[1]} {r.capitalize()}"
            f"{' ◀' if r == headline_region else ''} "
            f"| **{psi[r]}** | {psi_category(psi[r])[0]} |"
            for r in REGIONS
        )
        legend = "\n".join(f"| {e} {label} | {rng} |" for e, label, rng in LEGEND_ROWS)
        banner = (
            f"> ⚠️ **Stale data** — live fetch failed: {stale_reason}\n\n"
            if stale_reason else ""
        )

        head = (
            f"{banner}"
            f"# {emoji} PSI {headline_psi}\n"
            f"**{category}** · {subtitle}\n"
            f"*🕐 Updated {updated}*\n\n"
        )
        advisory = f"### NEA advisory\n\n{advice_markdown(category)}\n\n"
        if map_id:
            return f"{head}![PSI by region](tg://photo?id={map_id})\n\n{advisory.rstrip()}"

        return (
            f"{head}"
            f"---\n\n"
            f"### Regional breakdown\n\n"
            f"| Region | PSI | Level |\n"
            f"|:--|--:|:--|\n"
            f"{rows}\n\n"
            f"{advisory}"
            f"### PSI guide\n\n"
            f"| Level | PSI range |\n"
            f"|:--|--:|\n"
            f"{legend}"
        )
    except (KeyError, IndexError, TypeError, ValueError):
        return "Error parsing PSI data. The API response format may have changed."
