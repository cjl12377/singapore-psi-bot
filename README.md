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

- **Webhook only.** Updates arrive at `/webhook`, authenticated with a `secret_token` generated at each boot. Requests without it get a 403.
- **Token-safe logging.** `httpx` logging is raised to WARNING, since Telegram API URLs contain the bot token.
- **Rate limiting.** A 30-second per-user cooldown on PSI requests, including the preview after a `/view` change. The first blocked request gets one "please wait" notice that deletes itself; further attempts in the same window are ignored.
- **Concurrency.** Updates are handled in parallel. Rendered maps are cached per reading (at most 56: one per planning area plus the plain map), and only one data.gov.sg fetch runs at a time, so a burst of requests at cache expiry makes one call, not many.
- **Stale data.** If data.gov.sg is unreachable, `/psi` serves the last cached reading with a banner explaining why. After a failed fetch the bot waits 60 seconds before retrying, so an outage doesn't make every request wait out the timeout.
- **Alert flapping guard.** A reading hovering on a band edge (100 ↔ 101) could otherwise alert every hour. Worsening alerts go out immediately; an improvement within 3 hours of the last alert waits until the reading settles.
- **Location privacy.** Coordinates are used once to find the planning area and are never logged or stored.

### Region mapping

1. The coordinates are matched to one of Singapore's 55 URA planning areas by point-in-polygon.
2. Areas in URA's Central and North-East regions (which NEA doesn't use) go to the NEA region whose label point is nearest the area's centroid.
3. Points within ~900 m of Singapore's coast that fall outside every outline (beaches, reclaimed land) snap to the closest planning area. Anything further out is treated as outside Singapore.

This is an approximation. To correct an assignment, edit `AREA_REGION` in `location.py`.

### Admin access

Two hidden commands are for a single admin:

- `/stats` shows usage analytics (active users in the last 24h, all-time users, retention, and new users per day).
- `/feedback_list [n]` lists the newest n feedback entries (default 10, max 50), each with the sender's @username, Telegram ID and the time in SGT. The admin also gets a DM for each new entry as it arrives.

Both commands:

- They only respond to the Telegram user whose numeric ID matches the `ADMIN_USER_ID` environment variable, and only in a private chat with the bot.
- Everyone else, and the admin in a group chat, gets no reply, the same as for a command the bot doesn't recognise.
- They aren't listed in the command menu registered at startup or in `/help`.

## Data sources

- [data.gov.sg real-time PSI API](https://api-open.data.gov.sg/v2/real-time/api/psi) (v2), which needs no API key.
- URA Master Plan planning-area boundaries, via [yinshanyang/singapore](https://github.com/yinshanyang/singapore), simplified to ~22 m.
- NEA's 24-hour PSI health advisory, used for the alert advice text.

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
