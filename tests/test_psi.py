import asyncio
import copy
import unittest
from unittest.mock import patch

import httpx

from tests import SAMPLE_DATA
import psi


class PsiCategoryTest(unittest.TestCase):
    def test_band_edges(self):
        cases = {0: "Good", 50: "Good", 51: "Moderate", 100: "Moderate", 101: "Unhealthy",
                 200: "Unhealthy", 201: "Very Unhealthy", 300: "Very Unhealthy",
                 301: "Hazardous", 999: "Hazardous"}
        for value, label in cases.items():
            self.assertEqual(psi.psi_category(value)[0], label, value)

    def test_out_of_range_is_unknown(self):
        self.assertEqual(psi.psi_category(-1), ("Unknown", "⚪"))


class HeadlineTest(unittest.TestCase):
    def test_worst_region_uses_psi(self):
        self.assertEqual(psi.worst_region(SAMPLE_DATA), ("east", 102))

    def test_headline_defaults_to_highest_pm25(self):
        self.assertEqual(psi.headline(SAMPLE_DATA, None), ("east", 102, 40))

    def test_headline_for_chosen_region(self):
        self.assertEqual(psi.headline(SAMPLE_DATA, "west"), ("west", 40, 12))

    def test_headline_without_pm25_falls_back_to_psi(self):
        data = copy.deepcopy(SAMPLE_DATA)
        del data["data"]["items"][0]["readings"]["pm25_one_hourly"]
        self.assertEqual(psi.headline(data, None), ("east", 102, None))


class FormattingTest(unittest.TestCase):
    def test_text_message(self):
        text = psi.format_psi_message(SAMPLE_DATA)
        self.assertIn("PSI 102 — Unhealthy", text)
        self.assertIn("East region, highest of 5", text)
        self.assertIn("5 Oct 2026, 12:00 PM SGT", text)
        self.assertIn("Minimise prolonged or strenuous", text)  # NEA advice for Unhealthy
        self.assertNotIn("Stale data", text)

    def test_region_message_and_stale_banner(self):
        text = psi.format_region_psi_message(SAMPLE_DATA, "Jurong West", "west", "timed out")
        self.assertIn("📍 Jurong West · West region", text)
        self.assertIn("PSI 40 — Good", text)
        self.assertIn("Stale data</b> — live fetch failed: timed out", text)

    def test_caption(self):
        text = psi.format_psi_caption(SAMPLE_DATA)
        self.assertIn("PM2.5 40 µg/m³", text)
        self.assertIn("PSI Health Warnings as per NEA", text)

    def test_rich_with_and_without_map(self):
        table = psi.format_psi_rich(SAMPLE_DATA)
        self.assertIn("| 🟠 East ◀ | **40** | 102 | Unhealthy |", table)
        self.assertIn("### PSI Categories", table)
        with_map = psi.format_psi_rich(SAMPLE_DATA, map_id="map")
        self.assertIn("![PSI by region](tg://photo?id=map)", with_map)
        self.assertNotIn("Regional breakdown", with_map)
        for md in (table, with_map):  # one level below the section headings
            self.assertIn("\n#### PSI Health Warnings as per NEA\n", md)

    def test_malformed_data_returns_error_text(self):
        bad = {"data": {"items": [{"readings": {}}]}}
        for fmt in (psi.format_psi_message, psi.format_psi_caption, psi.format_psi_rich):
            self.assertIn("Error parsing PSI data", fmt(bad))
        self.assertEqual(psi.format_psi_message({"data": {"items": []}}),
                         "No PSI readings are currently available.")

    def test_advice_blocks(self):
        self.assertEqual(psi.advice_block("Good"), "• Normal activities for everyone.")
        self.assertEqual(psi.advice_markdown("Hazardous").count("\n- "), 2)  # three groups

    def test_bad_timestamp_passes_through(self):
        self.assertEqual(psi._fmt_timestamp("not a date"), "not a date")


class ClassifyErrorTest(unittest.TestCase):
    def test_messages(self):
        req = httpx.Request("GET", "https://x")
        self.assertEqual(psi._classify_error(httpx.ReadTimeout("t", request=req)),
                         "request to data.gov.sg timed out")
        self.assertEqual(psi._classify_error(httpx.ConnectError("c")),
                         "could not connect to data.gov.sg")
        err = httpx.HTTPStatusError("e", request=req, response=httpx.Response(429, request=req))
        self.assertEqual(psi._classify_error(err), "data.gov.sg returned HTTP 429")
        self.assertEqual(psi._classify_error(ValueError()), "unexpected error: ValueError")


def _ok_response(url):
    return httpx.Response(200, json=copy.deepcopy(SAMPLE_DATA), request=httpx.Request("GET", url))


class GetPsiDataTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        psi._cache.update(data=None, timestamp=0.0, stale_reason=None, failed_at=0.0)
        psi._fetch_lock = asyncio.Lock()  # bind to this test's event loop
        self.calls = []

    async def _fake_get(self, url, **kw):  # bound method: not passed the client
        self.calls.append(url)
        return _ok_response(url)

    async def test_fresh_fetch_merges_pm25_then_serves_cache(self):
        with patch.object(httpx.AsyncClient, "get", self._fake_get):
            data, reason = await psi.get_psi_data()
            again, _ = await psi.get_psi_data()
        self.assertIsNone(reason)
        self.assertIs(data, again)
        self.assertEqual(self.calls, [psi.PSI_API_URL, psi.PM25_API_URL])  # second call cached
        self.assertIn("pm25_one_hourly", data["data"]["items"][0]["readings"])

    async def test_burst_at_cache_expiry_fetches_once(self):
        async def slow_get(client, url, **kw):
            self.calls.append(url)
            await asyncio.sleep(0.05)
            return _ok_response(url)

        with patch.object(httpx.AsyncClient, "get", slow_get):
            results = await asyncio.gather(*[psi.get_psi_data() for _ in range(50)])
        self.assertEqual(len(self.calls), 2)  # one PSI + one PM2.5 call, not 100
        self.assertTrue(all(r[1] is None for r in results))

    async def test_failure_without_cache_reports_reason(self):
        async def down(client, url, **kw):
            raise httpx.ConnectError("down")

        with patch.object(httpx.AsyncClient, "get", down):
            self.assertEqual(await psi.get_psi_data(), (None, "could not connect to data.gov.sg"))

    async def test_failure_serves_stale_cache(self):
        with patch.object(httpx.AsyncClient, "get", self._fake_get):
            fresh, _ = await psi.get_psi_data()
        psi._cache["timestamp"] -= psi.CACHE_TTL + 1

        async def down(client, url, **kw):
            raise httpx.ConnectError("down")

        with patch.object(httpx.AsyncClient, "get", down):
            data, reason = await psi.get_psi_data()
        self.assertIs(data, fresh)
        self.assertEqual(reason, "could not connect to data.gov.sg")

    async def test_no_retry_within_backoff_then_retry_after(self):
        async def down(client, url, **kw):
            self.calls.append(url)
            raise httpx.ConnectError("down")

        with patch.object(httpx.AsyncClient, "get", down):
            await asyncio.gather(*[psi.get_psi_data() for _ in range(20)])
        self.assertEqual(len(self.calls), 1)  # outage: one attempt, the rest back off

        with patch.object(httpx.AsyncClient, "get", self._fake_get):
            self.assertIsNone((await psi.get_psi_data())[0])  # still backing off
            psi._cache["failed_at"] -= psi.RETRY_AFTER_FAILURE + 1
            data, reason = await psi.get_psi_data()
        self.assertIsNotNone(data)
        self.assertIsNone(reason)

    async def test_pm25_failure_is_best_effort(self):
        async def psi_only(client, url, **kw):
            if url == psi.PM25_API_URL:
                raise httpx.ConnectError("down")
            data = copy.deepcopy(SAMPLE_DATA)
            del data["data"]["items"][0]["readings"]["pm25_one_hourly"]
            return httpx.Response(200, json=data, request=httpx.Request("GET", url))

        with patch.object(httpx.AsyncClient, "get", psi_only):
            data, reason = await psi.get_psi_data()
        self.assertIsNone(reason)
        self.assertNotIn("pm25_one_hourly", data["data"]["items"][0]["readings"])


if __name__ == "__main__":
    unittest.main()
