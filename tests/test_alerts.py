import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import Forbidden, RetryAfter, TimedOut

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


class CurrentReadingTest(unittest.IsolatedAsyncioTestCase):
    async def test_includes_publish_time(self):
        from datetime import datetime
        with patch.object(alerts, "get_psi_data", AsyncMock(return_value=(tests.SAMPLE_DATA, None))):
            category, value, region, published = await alerts.current_reading()
        self.assertEqual((category, value, region), ("Unhealthy", 102, "east"))
        self.assertEqual(published, datetime.fromisoformat("2026-10-05T12:00:00+08:00").timestamp())

    async def test_missing_publish_time_is_none(self):
        import copy
        data = copy.deepcopy(tests.SAMPLE_DATA)
        del data["data"]["items"][0]["updatedTimestamp"]
        with patch.object(alerts, "get_psi_data", AsyncMock(return_value=(data, None))):
            self.assertIsNone((await alerts.current_reading())[3])


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

    async def _run(self, subs, send, reading=("Unhealthy", 120, "east", None)):
        fake = FakeRedis(subs)
        self.sleep = AsyncMock()
        with patch.object(alerts, "redis_client", fake), \
             patch.object(alerts, "current_reading", AsyncMock(return_value=reading)), \
             patch.object(alerts, "unsubscribe", AsyncMock()) as unsub, \
             patch.object(alerts.analytics, "record_alert_check", AsyncMock()) as self.tally, \
             patch.object(alerts.asyncio, "sleep", self.sleep):
            await alerts.run_alert_check(self._context(send))
        return fake, unsub

    async def test_sends_on_change_and_records_it(self):
        send = AsyncMock()
        fake, _ = await self._run({"1": {"chat_id": "1", "last_category": "Moderate"},
                                   "2": {"chat_id": "2", "last_category": "Unhealthy"}}, send)
        send.assert_awaited_once()
        self.assertEqual(send.await_args.args[0], 1)
        self.assertEqual(fake.subs["1"]["last_category"], "Unhealthy")
        self.tally.assert_awaited_once_with(["1"], 0, 0, 0, None)  # no publish time -> no lag

    async def test_tallies_lag_holds_and_blocks(self):
        published = alerts.time.time() - 600
        subs = {"1": {"chat_id": "1", "last_category": "Unhealthy"},           # improving: alert
                "2": {"chat_id": "2", "last_category": "Unhealthy",
                      "last_alert_at": str(alerts.time.time())},                # improving: held
                "3": {"chat_id": "3", "last_category": "Good"}}                 # worsening: alert
        send = AsyncMock(side_effect=lambda chat_id, *a, **k: None)
        await self._run(subs, send, reading=("Moderate", 80, "east", published))
        sent_to, failed, blocked, held, lag = self.tally.await_args.args
        self.assertEqual((sorted(sent_to), failed, blocked, held), (["1", "3"], 0, 0, 1))
        self.assertAlmostEqual(lag, 600, delta=5)  # only the worsening alert sets lag

    async def test_blocked_send_is_tallied(self):
        await self._run({"1": {"chat_id": "1", "last_category": "Moderate"}},
                        AsyncMock(side_effect=Forbidden("blocked")))
        self.tally.assert_awaited_once_with([], 0, 1, 0, None)

    async def test_nothing_to_tally_skips_the_write(self):
        await self._run({"1": {"chat_id": "1", "last_category": "Unhealthy"}}, AsyncMock())
        self.tally.assert_not_awaited()

    async def test_failed_send_is_retried_next_check(self):
        send = AsyncMock(side_effect=TimedOut())
        fake, _ = await self._run({"1": {"chat_id": "1", "last_category": "Moderate"}}, send)
        self.assertEqual(fake.subs["1"]["last_category"], "Moderate")  # not marked delivered

    async def test_rate_limited_send_waits_and_retries_once(self):
        send = AsyncMock(side_effect=[RetryAfter(5), None])
        fake, _ = await self._run({"1": {"chat_id": "1", "last_category": "Moderate"}}, send)
        self.assertEqual(send.await_count, 2)
        self.sleep.assert_any_await(5)
        self.assertEqual(fake.subs["1"]["last_category"], "Unhealthy")

    async def test_still_rate_limited_is_left_for_next_check(self):
        send = AsyncMock(side_effect=RetryAfter(5))
        fake, _ = await self._run({"1": {"chat_id": "1", "last_category": "Moderate"}}, send)
        self.assertEqual(send.await_count, 2)  # one retry, not a loop
        self.assertEqual(fake.subs["1"]["last_category"], "Moderate")

    async def test_sends_are_paced(self):
        subs = {str(i): {"chat_id": str(i), "last_category": "Moderate"} for i in range(3)}
        await self._run(subs, AsyncMock())
        paces = [c for c in self.sleep.await_args_list if c.args == (alerts.SEND_INTERVAL_SECS,)]
        self.assertEqual(len(paces), 3)
        self.assertLessEqual(1 / alerts.SEND_INTERVAL_SECS, 25)  # under Telegram's ~30/s

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
             patch.object(alerts, "current_reading", AsyncMock(return_value=("Good", 30, "east", None))):
            await alerts.run_alert_check(self._context(AsyncMock()))
        self.assertNotIn("9", fake.users)


if __name__ == "__main__":
    unittest.main()
