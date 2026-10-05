import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import Forbidden, RetryAfter

import tests  # noqa: F401  (sets the environment)
import alerts

H = 3600


class ShouldAlertTest(unittest.TestCase):
    def test_no_change(self):
        self.assertFalse(alerts.should_alert("Good", "Good", 0, 10 * H))

    def test_worsening_always_alerts(self):
        self.assertTrue(alerts.should_alert("Moderate", "Unhealthy", 10 * H, 10 * H))

    def test_improvement_waits_for_cooldown(self):
        self.assertFalse(alerts.should_alert("Unhealthy", "Moderate", 10 * H, 12 * H))
        self.assertTrue(alerts.should_alert("Unhealthy", "Moderate", 10 * H, 13 * H))


class OpeningLineTest(unittest.TestCase):
    def test_lines(self):
        self.assertIsNone(alerts.opening_line("Good", "Good"))
        self.assertEqual(alerts.opening_line("Moderate", "Unhealthy"), alerts.ESCALATION_LINES["Unhealthy"])
        self.assertIn("still Unhealthy", alerts.opening_line("Very Unhealthy", "Unhealthy"))
        self.assertEqual(alerts.opening_line("Unhealthy", "Moderate"), alerts.ALL_CLEAR_LINE)
        self.assertEqual(alerts.opening_line("Good", "Moderate"), alerts.MILD_CHANGE_LINE)

    def test_format_alert(self):
        text = alerts.format_alert("Moderate", "Unhealthy", 120, "east")
        self.assertIn("Air quality is now Unhealthy", text)
        self.assertIn("PSI 120 (East, highest of 5) — was Moderate", text)
        self.assertIn("/alert to turn these off", text)


class FakeRedis:
    """Just enough of redis.asyncio for alerts.run_alert_check."""

    def __init__(self, subs):
        self.subs = subs  # user_id -> hash
        self.users = set(subs)

    async def smembers(self, key):
        return set(self.users)

    async def hgetall(self, key):
        return dict(self.subs.get(key.removeprefix("psi:alert:"), {}))

    async def hset(self, key, mapping):
        self.subs[key.removeprefix("psi:alert:")].update({k: str(v) for k, v in mapping.items()})

    async def srem(self, key, uid):
        self.users.discard(uid)


class RunAlertCheckTest(unittest.IsolatedAsyncioTestCase):
    def _context(self, send):
        return SimpleNamespace(bot=SimpleNamespace(send_message=send))

    async def _run(self, subs, send, reading=("Unhealthy", 120, "east")):
        fake = FakeRedis(subs)
        with patch.object(alerts, "redis_client", fake), \
             patch.object(alerts, "current_reading", AsyncMock(return_value=reading)), \
             patch.object(alerts, "unsubscribe", AsyncMock()) as unsub:
            await alerts.run_alert_check(self._context(send))
        return fake, unsub

    async def test_sends_on_change_and_records_it(self):
        send = AsyncMock()
        fake, _ = await self._run({"1": {"chat_id": "1", "last_category": "Moderate"},
                                   "2": {"chat_id": "2", "last_category": "Unhealthy"}}, send)
        send.assert_awaited_once()
        self.assertEqual(send.await_args.args[0], 1)
        self.assertEqual(fake.subs["1"]["last_category"], "Unhealthy")

    async def test_failed_send_is_retried_next_check(self):
        send = AsyncMock(side_effect=RetryAfter(5))
        fake, _ = await self._run({"1": {"chat_id": "1", "last_category": "Moderate"}}, send)
        self.assertEqual(fake.subs["1"]["last_category"], "Moderate")  # not marked delivered

    async def test_blocked_user_is_unsubscribed(self):
        send = AsyncMock(side_effect=Forbidden("blocked"))
        _, unsub = await self._run({"1": {"chat_id": "1", "last_category": "Moderate"}}, send)
        unsub.assert_awaited_once_with("1")

    async def test_stale_or_missing_data_sends_nothing(self):
        send = AsyncMock()
        await self._run({"1": {"chat_id": "1", "last_category": "Moderate"}}, send, reading=None)
        send.assert_not_awaited()

    async def test_orphaned_user_id_is_cleaned_up(self):
        fake = FakeRedis({})
        fake.users.add("9")
        with patch.object(alerts, "redis_client", fake), \
             patch.object(alerts, "current_reading", AsyncMock(return_value=("Good", 30, "east"))):
            await alerts.run_alert_check(self._context(AsyncMock()))
        self.assertNotIn("9", fake.users)


if __name__ == "__main__":
    unittest.main()
