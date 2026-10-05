---
name: psi-rich-format
description: How the singapore-psi-bot formats /psi readings as Telegram rich messages (sendRichMessage, Bot API 9.5+) — layout, emoji/band conventions, the Markdown template, and the HTML fallback. Use when changing how PSI readings look or adding new PSI-style reports.
---

# PSI rich-message formatting

`/psi` sends a **rich-tier** message (real heading, real tables) via `sendRichMessage`, and falls back to the older HTML `sendMessage` if Telegram rejects it. Background on the two tiers and the API shape: `references/telegram-rich-message-spec.md`, `references/direct-sendrichmessage-recipe.md`.

## Where the code lives
- `psi.py` → `format_psi_rich()` builds the Markdown; `format_psi_message()` / `format_region_psi_message()` are the HTML fallback.
- `bot.py` → `_send_rich()` posts to `sendRichMessage` with httpx (python-telegram-bot 21.6 has no wrapper); `_deliver_psi()` tries rich first, then HTML.

## Layout (top to bottom)
1. Stale banner (only if live fetch failed) — `> ⚠️ **Stale data** — …` blockquote.
2. `# {band emoji} PM2.5 {value} µg/m³` — the headline is the 1-hr PM2.5 (separate `/v2/real-time/api/pm25` endpoint, merged into readings as `pm25_one_hourly` by `psi._attach_pm25`). Emoji/band and health advice still come from that region's 24-hr PSI. If the PM2.5 fetch fails, falls back to `# {emoji} PSI {value}`.
3. `**1-hour reading** · {subtitle}` then `**PSI {value} · {Band}** (24-hr)`; headline region = highest 1-hr PM2.5 — worst region ("highest of 5") for `/psi`, or `📍 {area} · {Region} region` for a location lookup.
4. `*🕐 Updated {timestamp}*`, then `---`.
5. `### Regional breakdown` — table: Region | PSI | Level. Headline region marked `◀`. (There used to be a 0–100 `█░` Scale bar; removed because it saturated at 100.)
6. `### PSI Categories` — band legend table (emoji + level, range right-aligned).

## Conventions
- Band emoji come from `PSI_BANDS`: 🟢 Good, 🟡 Moderate, 🟠 Unhealthy, 🔴 Very Unhealthy, 🟣 Hazardous. Use `psi_category()`; never hardcode thresholds elsewhere.
- Numbers bold; tables use alignment row `|:--|--:|:--|:--|` (numbers right-aligned).
- Casual tone; do NOT paraphrase NEA advice or regulatory wording — ask the user for verbatim text before adding any health-advice copy.
- Keep to constructs verified in the spec: headings, tables, blockquote, `---`, bold/italic, inline code. Expandable blockquotes / `<details>` are unverified for this bot — test on a live send first.
- Rich message limits: 32,768 chars; this output is ~700.

## Failure modes
- `sendRichMessage` non-200 or `ok: false` → `_send_rich` logs a warning and returns False → HTML fallback is sent. If users report the old look, check logs for `sendRichMessage rejected`.
- Old Telegram clients may not render rich messages; the fallback does not help there (server accepted the send).
- Never log the request URL — it contains the bot token (httpx logging is already silenced).

## Changing the design
Edit `format_psi_rich()`, then render locally with a sample payload (`{"data":{"items":[{"updatedTimestamp":"…","readings":{"psi_twenty_four_hourly":{"central":62,…}}}]}}`) and eyeball the Markdown; a real Telegram send is the only true render check.

## Map (primary /psi output)
`/psi` embeds a PNG map (`psi_map.render_psi_map`, Pillow) inside the rich message, replacing the regional table and guide (the legend is drawn on the image). The upload is multipart on `sendRichMessage`:
- `rich_message` JSON: `{"markdown": "...![PSI by region](tg://photo?id=map)", "media": [{"id": "map", "media": {"type": "photo", "media": "attach://map.png"}}]}`, plus a `map.png` file part. Verified live; the `id` field and nested `media` object are required (error text: `Can't find field "id"`, `Field "media" must be of type Object`).
- `attach://` directly in the markdown fails with `RICH_MESSAGE_PHOTO_URL_INVALID`; it must go through `tg://photo?id=`.
- Fallback chain in `_deliver_psi`: rich + map -> `sendPhoto` + HTML caption (`format_psi_caption`) -> rich table -> HTML.
- Badges: big number = 1-hr PM2.5, small `PSI n` line inside the badge = 24-hr PSI; fills/badge colours stay on the PSI band. Legend strip (`_draw_legend`): a grey sample badge ("PM2.5" over "PSI") labelled "1-hr PM2.5 (µg/m³)" / "24-hr PSI (rolling)", then a segmented PSI colour bar (range inside, level name under).
- Size: the PNG is delivered at 720 px wide as an 8-bit palette image (~21 KB; `_to_palette` keeps every flat colour exact, because plain median-cut merges the severity badge colours).
- Users choose map vs text via `/view` (`prefs.py`, Redis hash `psi:pref:{user_id}`, default map). Text view skips rendering and sends the rich table.
- Health warnings: `### PSI Health Warnings as per NEA` section (map view: below the map; text view: between the table and the guide) for the headline category, from `psi.ADVICE` — verbatim NEA wording, shared with the alerts (`alerts.format_alert`). Also in the photo caption and HTML fallback. Never reword it.
