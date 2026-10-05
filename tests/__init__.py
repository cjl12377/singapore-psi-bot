"""Unit tests. Run from the repo root: python -m unittest discover -s tests -t .

The bot modules read their config from the environment at import time, so dummy
values are set here, before any test imports them. Nothing in the suite talks to
Telegram, Redis or data.gov.sg: every network call is mocked.
"""
import os

os.environ.update(
    BOT_TOKEN="123:test-token",
    WEBHOOK_URL="https://example.invalid",
    REDIS_URL="redis://localhost:6399",  # never connected to; calls are mocked
    ADMIN_USER_ID="42",
)

# A reading in the shape of data.gov.sg's v2 PSI response, with PM2.5 merged in.
SAMPLE_DATA = {"data": {"items": [{
    "updatedTimestamp": "2026-10-05T12:00:00+08:00",
    "readings": {
        "psi_twenty_four_hourly": {"central": 55, "north": 48, "south": 60, "east": 102, "west": 40},
        "pm25_one_hourly": {"central": 20, "north": 15, "south": 22, "east": 40, "west": 12},
    },
}]}}
