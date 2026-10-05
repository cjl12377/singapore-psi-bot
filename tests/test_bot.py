import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.constants import ChatType
from telegram.ext import Application

from tests import SAMPLE_DATA
import analytics
import bot
import prefs


def fake_update(uid=1, chat_type=ChatType.PRIVATE, callback_data=None):
    chat = SimpleNamespace(
        id=uid, type=chat_type,
        send_message=AsyncMock(return_value=SimpleNamespace(chat_id=uid, message_id=99)),
    )
    query = SimpleNamespace(data=callback_data, answer=AsyncMock(), edit_message_text=AsyncMock())
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid), effective_chat=chat,
                           message=SimpleNamespace(reply_text=AsyncMock(), location=None),
                           callback_query=query)


def fake_context():
    tasks = []

    def create_task(coro):
        tasks.append(coro)
        coro.close()  # the delete-later sleep isn't needed in tests

    return SimpleNamespace(bot=SimpleNamespace(token="123:test-token", delete_message=AsyncMock()),
                           application=SimpleNamespace(create_task=create_task), tasks=tasks)


class DeliverPsiTest(unittest.IsolatedAsyncioTestCase):
    """Cooldown, /view preview and delivery fallbacks in bot._deliver_psi."""

    def setUp(self):
        bot._user_last_request.clear()
        bot._user_warned.clear()
        self.sent = []
        self.patches = [
            patch.object(bot, "get_psi_data", AsyncMock(return_value=(SAMPLE_DATA, None))),
            patch.object(bot.analytics, "track_request", AsyncMock()),
            patch.object(bot.prefs, "get_view", AsyncMock(return_value=prefs.VIEW_TEXT)),
            patch.object(bot, "_send_rich", AsyncMock(side_effect=self._record)),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    async def _record(self, *args, **kw):
        self.sent.append(args)
        return True

    async def test_first_request_is_served_and_tracked(self):
        u, c = fake_update(7), fake_context()
        await bot._deliver_psi(u, c, None)
        self.assertEqual(len(self.sent), 1)
        bot.analytics.track_request.assert_awaited_once_with("7")

    async def test_repeat_gets_one_notice_then_silence(self):
        u, c = fake_update(7), fake_context()
        for _ in range(5):
            await bot._deliver_psi(u, c, None)
        self.assertEqual(len(self.sent), 1)
        u.effective_chat.send_message.assert_awaited_once()  # one notice, not four
        self.assertIn("Please wait 30s", u.effective_chat.send_message.await_args.args[0])
        self.assertEqual(len(c.tasks), 1)  # one delete scheduled, no per-second edits

    async def test_notice_returns_after_window_expires(self):
        u, c = fake_update(7), fake_context()
        await bot._deliver_psi(u, c, None)
        await bot._deliver_psi(u, c, None)
        bot._user_last_request["7"] -= bot.USER_COOLDOWN_SECS + 1
        await bot._deliver_psi(u, c, None)  # served again
        await bot._deliver_psi(u, c, None)  # new window -> new notice
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(u.effective_chat.send_message.await_count, 2)

    async def test_view_preview_respects_cooldown(self):
        u, c = fake_update(7), fake_context()
        for _ in range(6):  # alternating Map/Text taps
            await bot._deliver_psi(u, c, None, preview=True)
        self.assertEqual(len(self.sent), 1)
        u.effective_chat.send_message.assert_not_awaited()  # previews are skipped silently
        bot.analytics.track_request.assert_not_awaited()     # previews aren't usage

    async def test_users_have_separate_cooldowns(self):
        c = fake_context()
        await bot._deliver_psi(fake_update(1), c, None)
        await bot._deliver_psi(fake_update(2), c, None)
        self.assertEqual(len(self.sent), 2)

    async def test_fetch_failure_message(self):
        bot.get_psi_data.return_value = (None, "could not connect to data.gov.sg")
        u = fake_update(7)
        await bot._deliver_psi(u, fake_context(), None)
        self.assertIn("could not connect to data.gov.sg",
                      u.effective_chat.send_message.await_args.args[0])

    async def test_falls_back_to_html_when_rich_fails(self):
        bot._send_rich.side_effect = None
        bot._send_rich.return_value = False
        u = fake_update(7)
        await bot._deliver_psi(u, fake_context(), None)
        self.assertIn("PSI 102 — Unhealthy", u.effective_chat.send_message.await_args.args[0])

    async def test_map_view_sends_rendered_png(self):
        bot.prefs.get_view.return_value = prefs.VIEW_MAP
        with patch.object(bot, "_render_map", AsyncMock(return_value=b"png")):
            await bot._deliver_psi(fake_update(7), fake_context(), None)
        self.assertEqual(self.sent[0][3], b"png")


class MapCacheTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot._map_cache.update(data=None, pngs={})

    async def test_renders_once_per_area_per_reading(self):
        render = unittest.mock.Mock(side_effect=lambda psi, area, pm25: f"png:{area}".encode())
        with patch.object(bot, "render_psi_map", render):
            a = await bot._render_map(SAMPLE_DATA, None)
            b = await bot._render_map(SAMPLE_DATA, None)
            await bot._render_map(SAMPLE_DATA, "Bedok")
            await bot._render_map(SAMPLE_DATA, "Bedok")
            self.assertIs(a, b)
            self.assertEqual(render.call_count, 2)

            newer = copy.deepcopy(SAMPLE_DATA)  # a new fetch is a new object
            await bot._render_map(newer, None)
            self.assertEqual(render.call_count, 3)
            self.assertEqual(list(bot._map_cache["pngs"]), [None])  # old maps dropped

    async def test_render_failure_returns_none_and_isnt_cached(self):
        with patch.object(bot, "render_psi_map", side_effect=RuntimeError):
            self.assertIsNone(await bot._render_map(SAMPLE_DATA, None))
        self.assertEqual(bot._map_cache["pngs"], {})

    async def test_real_render_produces_png(self):
        png = await bot._render_map(SAMPLE_DATA, "Bedok")
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertLess(len(png), 60_000)


class StatsAccessTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_admin_in_private_chat(self):
        stub = dict(active_users_24h=AsyncMock(return_value=3),
                    total_unique_users=AsyncMock(return_value=10),
                    retained_users=AsyncMock(return_value=2),
                    daily_growth=AsyncMock(return_value=[("2026-10-05", 1)]))
        cases = [(1, ChatType.PRIVATE, False), (42, ChatType.GROUP, False),
                 (42, ChatType.SUPERGROUP, False), (42, ChatType.CHANNEL, False),
                 (42, ChatType.PRIVATE, True)]
        with patch.multiple(bot.analytics, **stub):
            for uid, chat_type, allowed in cases:
                u = fake_update(uid, chat_type)
                await bot.cmd_stats(u, fake_context())
                self.assertEqual(u.message.reply_text.await_count, int(allowed), (uid, chat_type))
                if allowed:
                    self.assertIn("All-time unique users: <b>10</b>",
                                  u.message.reply_text.await_args.args[0])

    def test_stats_not_in_help_text(self):
        self.assertNotIn("/stats", bot.COMMANDS_TEXT)


class ViewButtonTest(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_unknown_value(self):
        u = fake_update(7, callback_data="view:evil")
        with patch.object(bot.prefs, "set_view", AsyncMock()) as set_view:
            await bot.on_view_button(u, fake_context())
        set_view.assert_not_awaited()

    async def test_saves_and_previews(self):
        u = fake_update(7, callback_data="view:text")
        with patch.object(bot.prefs, "set_view", AsyncMock()) as set_view, \
             patch.object(bot, "_deliver_psi", AsyncMock()) as deliver:
            await bot.on_view_button(u, fake_context())
        set_view.assert_awaited_once_with("7", "text")
        self.assertTrue(deliver.await_args.kwargs["preview"])

    async def test_save_failure_tells_user(self):
        u = fake_update(7, callback_data="view:map")
        with patch.object(bot.prefs, "set_view", AsyncMock(side_effect=ConnectionError)):
            await bot.on_view_button(u, fake_context())
        self.assertTrue(u.callback_query.answer.await_args.kwargs["show_alert"])


class LocationTest(unittest.IsolatedAsyncioTestCase):
    async def test_outside_singapore(self):
        u = fake_update(7)
        u.message.location = SimpleNamespace(latitude=51.5, longitude=-0.12)
        await bot.on_location(u, fake_context())
        self.assertIn("doesn't look like it's in Singapore", u.message.reply_text.await_args.args[0])

    async def test_inside_singapore_delivers_region(self):
        u = fake_update(7)
        u.message.location = SimpleNamespace(latitude=1.3236, longitude=103.9273)
        with patch.object(bot, "_deliver_psi", AsyncMock()) as deliver:
            await bot.on_location(u, fake_context())
        self.assertEqual(deliver.await_args.kwargs["place"], ("Bedok", "east"))


class KeyboardTest(unittest.IsolatedAsyncioTestCase):
    """The pinned 🌫 Check PSI / 📍 Share location (mobile) bar."""

    def test_layout(self):
        (psi_btn, loc_btn), = bot.MAIN_KEYBOARD.keyboard
        self.assertEqual(psi_btn.text, bot.PSI_BUTTON)
        self.assertTrue(loc_btn.request_location)
        self.assertTrue(bot.MAIN_KEYBOARD.is_persistent)
        self.assertTrue(bot.MAIN_KEYBOARD.resize_keyboard)
        self.assertFalse(bot.MAIN_KEYBOARD.one_time_keyboard)

    def test_private_chats_only(self):
        self.assertIs(bot._keyboard(fake_update(1, ChatType.PRIVATE)), bot.MAIN_KEYBOARD)
        for chat_type in (ChatType.GROUP, ChatType.SUPERGROUP):
            self.assertIsNone(bot._keyboard(fake_update(1, chat_type)))

    def test_markup_param_is_json(self):
        import json
        self.assertEqual(bot._markup_param(None), {})
        sent = json.loads(bot._markup_param(bot.MAIN_KEYBOARD)["reply_markup"])
        self.assertTrue(sent["is_persistent"])
        self.assertEqual(sent["keyboard"][0][1]["text"], bot.LOCATION_BUTTON)

    async def test_start_and_help_attach_bar_in_private_only(self):
        for handler in (bot.cmd_start, bot.cmd_help):
            u = fake_update(1, ChatType.PRIVATE)
            await handler(u, fake_context())
            self.assertIs(u.message.reply_text.await_args.kwargs["reply_markup"], bot.MAIN_KEYBOARD)
            g = fake_update(1, ChatType.GROUP)
            await handler(g, fake_context())
            self.assertIsNone(g.message.reply_text.await_args.kwargs["reply_markup"])

    async def test_location_command(self):
        u = fake_update(1, ChatType.PRIVATE)
        await bot.cmd_location(u, fake_context())
        self.assertIs(u.message.reply_text.await_args.kwargs["reply_markup"], bot.MAIN_KEYBOARD)
        self.assertIn("📎 → Location", u.message.reply_text.await_args.args[0])
        g = fake_update(1, ChatType.GROUP)
        await bot.cmd_location(g, fake_context())
        self.assertIn("private chat", g.message.reply_text.await_args.args[0])
        self.assertNotIn("reply_markup", g.message.reply_text.await_args.kwargs)

    async def test_psi_reply_carries_bar(self):
        bot._user_last_request.clear()
        with patch.object(bot, "get_psi_data", AsyncMock(return_value=(SAMPLE_DATA, None))), \
             patch.object(bot.analytics, "track_request", AsyncMock()), \
             patch.object(bot.prefs, "get_view", AsyncMock(return_value=prefs.VIEW_MAP)), \
             patch.object(bot, "_render_map", AsyncMock(return_value=b"png")), \
             patch.object(bot, "_send_rich", AsyncMock(return_value=False)) as rich, \
             patch.object(bot, "_send_map", AsyncMock(return_value=False)) as photo:
            u = fake_update(5, ChatType.PRIVATE)
            await bot._deliver_psi(u, fake_context(), None)
        for call in rich.await_args_list + photo.await_args_list:
            self.assertIs(call.kwargs["reply_markup"], bot.MAIN_KEYBOARD)
        # every tier failed -> plain HTML, still with the bar
        self.assertIs(u.effective_chat.send_message.await_args.kwargs["reply_markup"], bot.MAIN_KEYBOARD)

    def test_psi_button_text_routes_to_psi(self):
        from telegram import Update as TgUpdate
        captured = {}
        with patch.object(Application, "run_webhook", lambda self, **kw: captured.update(app=self)):
            bot.main()
        app = captured["app"]

        def handler_for(text, chat_type="private"):
            upd = TgUpdate.de_json({"update_id": 1, "message": {
                "message_id": 1, "date": 0, "text": text,
                "chat": {"id": 5, "type": chat_type}, "from": {"id": 5, "is_bot": False, "first_name": "u"},
            }}, app.bot)
            return next((h.callback for h in app.handlers[0] if h.check_update(upd)), None)

        self.assertIs(handler_for(bot.PSI_BUTTON), bot.cmd_psi)
        self.assertIsNone(handler_for("Check PSI please"))           # other text is ignored
        self.assertIsNone(handler_for(bot.PSI_BUTTON, "supergroup"))  # private chats only


class StartupTest(unittest.TestCase):
    def test_main_enables_concurrent_updates(self):
        captured = {}
        with patch.object(Application, "run_webhook", lambda self, **kw: captured.update(app=self, **kw)):
            bot.main()
        self.assertGreater(captured["app"].update_processor.max_concurrent_updates, 1)
        self.assertEqual(captured["secret_token"], bot.WEBHOOK_SECRET)  # webhook is authenticated


class AnalyticsTest(unittest.IsolatedAsyncioTestCase):
    async def test_retained_users_counts_spaced_sessions(self):
        h = 3600
        history = {
            "a": [0, 13 * h, 26 * h, 39 * h],          # 4 sessions >12h apart -> retained
            "b": [0, 1 * h, 2 * h, 3 * h, 4 * h],      # many requests, one session
            "c": [],
        }
        fake = SimpleNamespace(
            smembers=AsyncMock(return_value=set(history)),
            zrange=AsyncMock(side_effect=lambda key, *a, **k:
                             [(str(t), t) for t in history[key.removeprefix("psi:requests:")]]),
        )
        with patch.object(analytics, "redis_client", fake):
            self.assertEqual(await analytics.retained_users(), 1)

    async def test_track_request_never_raises(self):
        broken = SimpleNamespace(pipeline=unittest.mock.Mock(side_effect=ConnectionError))
        with patch.object(analytics, "redis_client", broken), self.assertLogs(level="ERROR"):
            await analytics.track_request("7")


class PrefsTest(unittest.IsolatedAsyncioTestCase):
    async def test_defaults_to_map_when_redis_down_or_value_bad(self):
        with patch.object(prefs, "redis_client", SimpleNamespace(hget=AsyncMock(side_effect=ConnectionError))):
            self.assertEqual(await prefs.get_view("7"), prefs.VIEW_MAP)
        with patch.object(prefs, "redis_client", SimpleNamespace(hget=AsyncMock(return_value="junk"))):
            self.assertEqual(await prefs.get_view("7"), prefs.VIEW_MAP)
        with patch.object(prefs, "redis_client", SimpleNamespace(hget=AsyncMock(return_value="text"))):
            self.assertEqual(await prefs.get_view("7"), prefs.VIEW_TEXT)


if __name__ == "__main__":
    unittest.main()
