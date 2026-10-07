import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import tests  # noqa: F401  (sets the environment)
import analytics


class FakeRedis:
    """In-memory stand-in for the redis.asyncio calls analytics.py makes. A pipeline
    queues calls and runs them in order on execute(), returning each result."""

    def __init__(self):
        self.data: dict = {}

    # strings
    async def get(self, k):
        return self.data.get(k)

    async def set(self, k, v, nx=False, ex=None):
        if nx and k in self.data:
            return None
        self.data[k] = str(v)
        return True

    async def delete(self, k):
        return int(self.data.pop(k, None) is not None)

    async def expire(self, k, secs):
        return int(k in self.data)

    # sets
    async def sadd(self, k, v):
        s = self.data.setdefault(k, set())
        added = str(v) not in s
        s.add(str(v))
        return int(added)

    async def srem(self, k, v):
        s = self.data.get(k, set())
        present = str(v) in s
        s.discard(str(v))
        return int(present)

    async def smembers(self, k):
        return set(self.data.get(k, set()))

    async def scard(self, k):
        return len(self.data.get(k, set()))

    # sorted sets
    async def zadd(self, k, mapping):
        self.data.setdefault(k, {}).update(mapping)

    async def zremrangebyrank(self, k, start, stop):
        return 0

    async def zcount(self, k, lo, hi):
        return sum(1 for score in self.data.get(k, {}).values() if score >= lo)

    async def zrange(self, k, start, stop, withscores=False):
        return sorted(self.data.get(k, {}).items(), key=lambda kv: kv[1])

    # hashes
    async def hincrby(self, k, field, n):
        h = self.data.setdefault(k, {})
        h[field] = str(int(h.get(field, 0)) + n)
        return int(h[field])

    async def hgetall(self, k):
        return dict(self.data.get(k, {}))

    async def hexists(self, k, field):
        return field in self.data.get(k, {})

    async def hlen(self, k):
        return len(self.data.get(k, {}))

    # lists
    async def lpush(self, k, v):
        self.data.setdefault(k, []).insert(0, str(v))

    async def ltrim(self, k, start, stop):
        self.data[k] = self.data.get(k, [])[start:stop + 1]

    async def lrange(self, k, start, stop):
        return list(self.data.get(k, []))

    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis):
        self.redis, self.calls = redis, []

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return queue

    async def execute(self):
        return [await getattr(self.redis, n)(*a, **k) for n, a, k in self.calls]


class PureHelpersTest(unittest.TestCase):
    def test_percentile(self):
        self.assertIsNone(analytics.percentile([], 50))
        self.assertEqual(analytics.percentile([5], 95), 5)
        values = list(range(1, 101))
        self.assertEqual(analytics.percentile(values, 50), 50)
        self.assertEqual(analytics.percentile(values, 95), 95)
        self.assertEqual(analytics.percentile(values, 100), 100)

    def test_cohort_return_counts_users_back_within_window(self):
        today = date(2026, 10, 20)
        d = today - timedelta(days=5)
        new = {d: {"a", "b", "c", "d"}}
        active = {d: {"a", "b", "c", "d"}, d + timedelta(days=1): {"a"},
                  d + timedelta(days=3): {"b", "x"}}
        since = today - timedelta(days=30)
        self.assertEqual(analytics.cohort_return(new, active, 1, today, since), (1, 4))
        # 7-day window for d isn't over yet (d + 7 > yesterday): cohort excluded, not counted as 0
        self.assertEqual(analytics.cohort_return(new, active, 7, today, since), (0, 0))
        d7 = today - timedelta(days=8)
        new7 = {d7: {"a", "b"}}
        active7 = {d7 + timedelta(days=7): {"a"}, d7 + timedelta(days=8): {"b"}}  # b: day 8, too late
        self.assertEqual(analytics.cohort_return(new7, active7, 7, today, since), (1, 2))

    def test_cohort_return_ignores_cohorts_before_tracking_began(self):
        today = date(2026, 10, 20)
        old = today - timedelta(days=10)
        new = {old: {"a", "b"}}
        self.assertEqual(analytics.cohort_return(new, {}, 1, today, since=old + timedelta(days=1)), (0, 0))
        self.assertEqual(analytics.cohort_return(new, {}, 1, today, since=None), (0, 0))

    def test_parse_command(self):
        p = analytics.parse_command
        self.assertEqual(p("/Weather", "Haze_SGbot"), "weather")
        self.assertEqual(p("/forecast tomorrow please", "Haze_SGbot"), "forecast")
        self.assertEqual(p("/forecast@haze_sgbot", "Haze_SGbot"), "forecast")
        self.assertIsNone(p("/forecast@OtherBot", "Haze_SGbot"))
        self.assertIsNone(p("/<b>x</b>", "Haze_SGbot"))
        self.assertIsNone(p("/" + "a" * 33, "Haze_SGbot"))
        self.assertIsNone(p("", "Haze_SGbot"))

    def test_chat_member_event(self):
        e = analytics.chat_member_event
        self.assertEqual(e("private", "member", "kicked"), "blocked")
        self.assertEqual(e("private", "kicked", "member"), "unblocked")
        self.assertIsNone(e("private", "member", "member"))
        self.assertEqual(e("group", "left", "member"), "group_add")
        self.assertEqual(e("supergroup", "left", "administrator"), "group_add")
        self.assertIsNone(e("supergroup", "member", "administrator"))  # promoted: still in
        self.assertEqual(e("supergroup", "administrator", "kicked"), "group_remove")
        self.assertIsNone(e("channel", "left", "administrator"))


class RecordingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fake = FakeRedis()
        p = patch.object(analytics, "redis_client", self.fake)
        p.start()
        self.addCleanup(p.stop)

    async def test_requests_feed_the_report(self):
        await analytics.track_request("1", ["psi", "sent_rich"], reply_ms=400, e2e_ms=1000)
        await analytics.track_request("2", ["psi", "psi_location", "sent_photo"], reply_ms=800)
        await analytics.track_request("1", ["psi", "sent_rich"], reply_ms=600, e2e_ms=3000)
        await analytics.count("cooldown")
        r = await analytics.report()
        self.assertEqual(r["since"], analytics._today())
        self.assertEqual((r["today_active"], r["today_new"], r["wau"], r["mau"]), (2, 2, 2, 2))
        self.assertEqual(r["total_users"], 2)
        self.assertEqual(r["events"]["psi"], 3)
        self.assertEqual(r["events"]["psi_location"], 1)
        self.assertEqual(r["events"]["cooldown"], 1)
        self.assertEqual(sorted(r["reply_ms"]), [400, 600, 800])
        self.assertEqual(sorted(r["e2e_ms"]), [1000, 3000])
        self.assertEqual(r["daily"][-1][1:], (2, 2))
        self.assertEqual(r["returns"][1], (0, 0))  # first day: no complete cohort yet

    async def test_active_since_is_set_once(self):
        self.fake.data[analytics.ACTIVE_SINCE_KEY] = "2026-01-01"
        await analytics.track_request("1")
        self.assertEqual(self.fake.data[analytics.ACTIVE_SINCE_KEY], "2026-01-01")

    async def test_alert_followup_counts_once(self):
        await analytics.record_alert_check(["1", "2"], failed=1, blocked=0, held=2, lag_secs=120)
        await analytics.track_request("1")
        await analytics.track_request("1")  # the marker was used up by the first check
        r = await analytics.report()
        ev = r["events"]
        self.assertEqual((ev["alert_sent"], ev["alert_failed"], ev["alert_held"]), (2, 1, 2))
        self.assertEqual(ev["alert_followup"], 1)
        self.assertEqual(r["alert_lags"], [120])

    async def test_unknown_commands_are_capped_per_day(self):
        with patch.object(analytics, "MAX_UNKNOWN_COMMAND_NAMES", 2):
            for name in ("a", "b", "c", "a"):
                await analytics.record_unknown_command(name)
        r = await analytics.report()
        self.assertEqual(r["events"]["unknown_cmd"], 4)  # every one counted in the total
        self.assertEqual(r["unknown"], [("a", 2), ("b", 1)])  # "c" arrived after the cap

    async def test_group_membership_is_tracked(self):
        await analytics.record_chat_member("group_add", -100)
        await analytics.record_chat_member("group_add", -200)
        await analytics.record_chat_member("group_remove", -100)
        self.assertEqual((await analytics.report())["groups"], 1)


class NeverRaisesTest(unittest.IsolatedAsyncioTestCase):
    async def test_writes_swallow_redis_errors(self):
        broken = SimpleNamespace(pipeline=Mock(side_effect=ConnectionError),
                                 hexists=AsyncMock(side_effect=ConnectionError))
        with patch.object(analytics, "redis_client", broken):
            for call in (analytics.track_request("7", ["psi"], reply_ms=1),
                         analytics.count("error"),
                         analytics.record_alert_check(["1"], 0, 0, 0, 5),
                         analytics.record_unknown_command("x"),
                         analytics.record_chat_member("blocked", 7)):
                with self.assertLogs(analytics.logger, "ERROR"):
                    await call


if __name__ == "__main__":
    unittest.main()
