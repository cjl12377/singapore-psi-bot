# Singapore PSI Bot

A Telegram bot that reports live Singapore air quality (PSI) from the National Environment Agency, with location lookups and change alerts.

Try it: [@Haze_SGbot](https://t.me/Haze_SGbot)

## Features

- **`/psi`** — headline 24-hour PSI (the worst of Singapore's 5 regions) and a colour-coded map of Singapore with each region's value (or a text table; see `/view`).
- **`/view`** — choose how `/psi` looks: the map (default, ~20 KB image) or a plain text table. Saved per user.
- **`/location`**, or just send a location — finds your URA planning area and shows the PSI for its region.
- **Button bar** — in private chats, **🌫 Check PSI** and **📍 Share location (mobile)** stay pinned under the message box. Telegram doesn't tell bots which device you're on, so desktop shows the location button too; its label says it only works in the phone apps (on desktop, use 📎 → Location).
- **`/alert`** — an on/off toggle. While on, the bot messages you whenever the PSI category changes (e.g. Moderate → Unhealthy), with NEA's health advice for the new level.
- **`/feedback`** — send feedback to the developer, inline (`/feedback the map is great`) or as your next message after a bare `/feedback`. Private chats only; up to 1,000 characters and 5 entries per minute per user.

## How it works

| File | Responsibility |
|---|---|
| `bot.py` | Command handlers, webhook server, command menu registration, alert job scheduling |
| `psi.py` | Fetches PSI from data.gov.sg, 10-minute cache to rate limit, stale-data fallback, message formatting |
| `location.py` + `planning_areas.json` | Point-in-polygon lookup from coordinates to planning area to PSI region |
| `alerts.py` | Alert subscriptions, the 30-minute category-change check, NEA advisory text |
| `analytics.py` | Redis-backed usage tracking for `/stats` |
| `feedback.py` | Feedback storage and formatting for `/feedback` and `/feedback_list` |

Design notes:

- **Webhook only.** Updates arrive at `/webhook`, authenticated with a `secret_token` derived from the bot token, so it stays the same across restarts and deploys. Requests without it get a 403.
- **Token-safe logging.** `httpx` logging is raised to WARNING, since Telegram API URLs contain the bot token.
- **Rate limiting.** A 30-second per-user cooldown on PSI requests, including the preview after a `/view` change. The first blocked request gets one "please wait" notice that deletes itself; further attempts in the same window are ignored.
- **Concurrency.** Updates are handled in parallel. Rendered maps are cached per reading (at most 56: one per planning area plus the plain map), and only one data.gov.sg fetch runs at a time, so a burst of requests at cache expiry makes one call, not many.
- **Stale data.** If data.gov.sg is unreachable, `/psi` serves the last cached reading with a banner explaining why. After a failed fetch the bot waits 60 seconds before retrying, so an outage doesn't make every request wait out the timeout.
- **Alert flapping guard.** A reading hovering on a band edge (100 ↔ 101) could otherwise alert every hour. Worsening alerts go out immediately; an improvement within 3 hours of the last alert waits until the reading settles.
- **Alert delivery.** Alerts go out at most 20 a second, under Telegram's ~30/s limit. If Telegram still asks the bot to slow down, it waits the time Telegram gives and retries once; anything that still fails is retried at the next check.
- **Error replies.** If a handler fails unexpectedly (e.g. Redis is unreachable), the error is logged and the user is told to try again, rather than getting no reply.
- **Location privacy.** Coordinates are used once to find the planning area and are never logged or stored.

### Region mapping

1. The coordinates are matched to one of Singapore's 55 URA planning areas by point-in-polygon.
2. Areas in URA's Central and North-East regions (which NEA doesn't use) go to the NEA region whose label point is nearest the area's centroid.
3. Points within ~900 m of Singapore's coast that fall outside every outline (beaches, reclaimed land) snap to the closest planning area. Anything further out is treated as outside Singapore.

This is an approximation. To correct an assignment, edit `AREA_REGION` in `location.py`.

### Admin access

Two hidden commands are for a single admin:

- `/stats` shows usage analytics. See [What `/stats` measures](#what-stats-measures).
- `/feedback_list [n]` lists the newest n feedback entries (default 10, max 50), each with the sender's @username, Telegram ID and the time in SGT. The admin also gets a DM for each new entry as it arrives.

Both commands:

- They only respond to the Telegram user whose numeric ID matches the `ADMIN_USER_ID` environment variable, and only in a private chat with the bot.
- Everyone else, and the admin in a group chat, gets no reply, the same as for a command the bot doesn't recognise.
- They aren't listed in the command menu registered at startup or in `/help`.

## Data sources

- [data.gov.sg real-time PSI API](https://api-open.data.gov.sg/v2/real-time/api/psi) (v2), which needs no API key.
- URA Master Plan planning-area boundaries, via [yinshanyang/singapore](https://github.com/yinshanyang/singapore), simplified to ~22 m.
- NEA's 24-hour PSI health advisory, used for the alert advice text.

## What `/stats` measures

A PSI request is `/psi`, the 🌫 Check PSI button, or a shared location. It counts once it gets past the 30-second cooldown. The preview after a `/view` change doesn't count. All analytics writes run in the background after the reply, and a failed write is logged and dropped, so a Redis outage never delays or breaks a reply.

**Users**
- **Active (24h)** is the number of people with a request in the rolling last 24 hours.
- **Today** counts people active on the Singapore calendar day, split into first-time and returning users.
- **Weekly and monthly active** count unique users over the last 7 and 30 calendar days. **Stickiness** is the average daily active count over the last 30 full days, divided by monthly active.
- **Regulars** is the original all-time measure: 4+ visits, each more than 12 hours after the last.

**Retention**
- Retention shows what share of new users came back the next day, within 7 days, and within 30 days.
- It covers the latest 14 daily cohorts whose window has fully passed.
- Day-by-day activity is only recorded from the first deploy of this tracking onwards (`psi:active_since`). Earlier cohorts are left out rather than shown as 0%.

**Usage**
- Counts of PSI checks (by location, in groups), locations outside Singapore, cooldown hits, `/view` changes, feedback, blocks and unblocks, and group adds and removals.
- **Unrecognised commands** are counted by name. Up to 100 names a day are kept; the total always counts. In groups, only `/cmd@ThisBot` counts.
- **Other text** is private messages that weren't feedback. Only the count is kept, never the text.

**Alerts**
- Sent, failed and blocked alerts, and alerts held back by the flap guard. A held alert is counted on each 30-minute check it's held for.
- **Follow-up rate** is the share of alerts followed by a PSI request from the same user within an hour. Telegram doesn't tell bots when a message is read.
- **Warning lag** is the time from NEA publishing a reading to the first *worsening* alert it caused, over the last 200 such alerts.

**Reliability**
- **Reply time in the bot** runs from the handler starting to the reply being sent.
- **Reply time end to end** runs from the user's message timestamp, which has one-second resolution. It includes cold starts and Telegram's delivery time.
- Also counted: which format each reply went out as (rich map, photo, rich text, plain text), stale-data replies, fetch failures, and error replies.

Not tracked yet: language, planning-area demand, peak load, and haze-day return rate.

## Redis keys

| Key | Type | Purpose |
|---|---|---|
| `psi:all_users` | Set | All-time unique users |
| `psi:last_seen` | Sorted set | Last request time per user (24h actives) |
| `psi:new_users:<YYYY-MM-DD>` | Set | New users per day (SGT) |
| `psi:requests:<user_id>` | Sorted set | Last 100 request timestamps per user (retention) |
| `psi:alert:<user_id>` | Hash | Alert subscription: `chat_id`, `last_category`, `last_alert_at` |
| `psi:alert_users` | Set | Users with alerts on |
| `psi:feedback` | List | Feedback entries, newest first, capped at 1,000: JSON with `user_id`, `username`, `text`, `ts` |
| `psi:active:<YYYY-MM-DD>` | Set | Users with a PSI request that day (SGT). Expires after 100 days |
| `psi:active_since` | String | The first day `psi:active:*` was recorded. Older cohorts are excluded from retention |
| `psi:events:<YYYY-MM-DD>` | Hash | Event name → count for that day (e.g. `psi`, `cooldown`, `alert_sent`, `sent_photo`). Expires after 100 days |
| `psi:reply_ms:<YYYY-MM-DD>` | List | Time in the handler per PSI reply, in ms, last 1,000 that day. Expires after 100 days |
| `psi:e2e_ms:<YYYY-MM-DD>` | List | Time from the user's message to the reply, in ms, last 1,000 that day. Expires after 100 days |
| `psi:unknown_cmds:<YYYY-MM-DD>` | Hash | Unrecognised command name → count, at most 100 names. Expires after 100 days |
| `psi:alert_lag` | List | Seconds from NEA's reading to the first worsening alert, last 200 |
| `psi:alerted:<user_id>` | String | Set when an alert is sent. Expires after 1 hour, and is used to measure follow-up |
| `psi:groups` | Set | Group chat IDs the bot has been added to (since tracking began) |

## Setup

Requirements: Python 3.12, a bot token from [@BotFather](https://t.me/BotFather), and a Redis database (a free tier from Redis Cloud or Upstash works).

```bash
pip install -r requirements.txt
```

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | yes | Token from BotFather |
| `WEBHOOK_URL` | yes | Public HTTPS base URL of the service, e.g. `https://your-app.onrender.com` |
| `REDIS_URL` | yes | `redis://` connection string |
| `ADMIN_USER_ID` | yes | Your numeric Telegram user ID, which unlocks `/stats` |
| `PORT` | no | Port to listen on (default 8443; Render sets this) |

See `.env.example`. The bot is webhook-only, so running it locally needs a public HTTPS URL (for example, a tunnel) as `WEBHOOK_URL`.

```bash
python bot.py
```

On startup the bot registers its webhook and its command menu with Telegram. It also sets the profile's About text and the intro shown in an empty chat from `SHORT_DESCRIPTION` and `DESCRIPTION` in `bot.py`. Edit them there, not in BotFather: BotFather edits are overwritten at the next deploy.

## Testing

```bash
python -m unittest discover -s tests -t .
```

The suite needs no network, Redis or bot token: Telegram, Redis and data.gov.sg are all mocked.

## Deploying on Render

1. Create a **Web Service** from this repo.
   - Build command: `pip install -r requirements.txt`
   - Start command: `python bot.py`
2. Set the environment variables above, plus `PYTHON_VERSION=3.12.7`. python-telegram-bot 21.6 fails on Python 3.14, and a manually created service doesn't reliably read `runtime.txt`. `render.yaml` documents the same settings for Blueprint deploys.
3. Free tier: the service sleeps after 15 minutes without traffic, which pauses the alert check. Keep it awake by pinging the root URL every 5 minutes, for example with UptimeRobot. The root URL returns 404, which is expected and still counts as traffic.

## License

[MIT](LICENSE)
