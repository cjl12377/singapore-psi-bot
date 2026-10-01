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
2. `# {band emoji} PSI {value}` — the headline number is the H1.
3. `**{Band}** · {subtitle}` — worst region ("highest of 5") for `/psi`, or `📍 {area} · {Region} region` for a location lookup.
4. `*🕐 Updated {timestamp}*`, then `---`.
5. `### Regional breakdown` — table: Region | PSI | Level | Scale. Headline region marked `◀`. Scale is a 10-cell `█░` bar on a 0–100 scale (capped), in backticks so it stays monospace.
6. `### PSI guide` — band legend table (emoji + level, range right-aligned).

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
`/psi` now leads with a PNG map (`psi_map.render_psi_map`, Pillow) sent via `sendPhoto` with an HTML caption from `format_psi_caption()`. Regions are tinted pale by band with a solid band-coloured value badge; location lookups outline the user's planning area. The rich table above is the fallback if rendering or `sendPhoto` fails.
