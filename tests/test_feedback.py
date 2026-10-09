import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import Update as TgUpdate
from telegram.constants import ChatType, ParseMode
from telegram.ext import Application, CommandHandler

import tests  # noqa: F401  (sets the environment)
import bot
import feedback

ADMIN = 42


class FakeRedis:
    """Just enough of redis.asyncio for feedback.py (lists + pipeline)."""

    def __init__(self, fail=False):
        self.items: list[str] = []
        self.fail = fail

    def pipeline(self):
        ops = []
        outer = self

        class Pipe:
            def lpush(self, key, value):
                ops.append(lambda: outer.items.insert(0, value))

            def ltrim(self, key, start, end):
                ops.append(lambda: outer.items.__delitem__(slice(end + 1, None)))

            async def execute(self):
                if outer.fail:
                    raise ConnectionError
                for op in ops:
                    op()

        return Pipe()

    async def lrange(self, key, start, end):
        return self.items[start:end + 1]

    async def llen(self, key):
        return len(self.items)


def fake_update(uid=1, text="/feedback", username="jane", chat_type=ChatType.PRIVATE):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=uid, username=username),
        effective_chat=SimpleNamespace(id=uid, type=chat_type),
        message=SimpleNamespace(text=text, reply_text=AsyncMock()),
    )


def fake_context(args=None):
    return SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()), args=args or [],
                           application=SimpleNamespace(create_task=lambda coro: coro.close()))


class FeedbackTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot._awaiting_feedback.clear()
        bot._feedback_times.clear()
        self.redis = FakeRedis()
        p = patch.object(feedback, "redis_client", self.redis)
        p.start()
        self.addCleanup(p.stop)

    async def stored(self):
        return await feedback.recent(1000)


class SubmitTest(FeedbackTestCase):
    async def test_inline_feedback_saved_with_metadata_and_dm(self):
        u, c = fake_update(7, "/feedback the map is great"), fake_context()
        with patch("time.time", return_value=1_791_207_000.0):
            await bot.cmd_feedback(u, c)
        (entry,) = await self.stored()
        self.assertEqual(entry, {"user_id": 7, "username": "jane", "text": "the map is great",
                                 "ts": 1_791_207_000.0})
        self.assertNotIn("name", entry)
        dm_chat, dm_text = c.bot.send_message.await_args.args
        self.assertEqual(dm_chat, ADMIN)
        self.assertIn("💬 <b>New feedback</b>", dm_text)
        self.assertIn("From: @jane · id 7", dm_text)
        self.assertIn("5 Oct 2026, 9:30 PM SGT", dm_text)
        self.assertTrue(dm_text.endswith("the map is great"))
        self.assertIn("Thanks", u.message.reply_text.await_args.args[0])

    async def test_keeps_line_breaks(self):
        await bot.cmd_feedback(fake_update(7, "/feedback line one\nline two"), fake_context())
        self.assertEqual((await self.stored())[0]["text"], "line one\nline two")

    async def test_no_username_shows_id_only(self):
        c = fake_context()
        await bot.cmd_feedback(fake_update(7, "/feedback hi", username=None), c)
        self.assertEqual((await self.stored())[0]["username"], "")
        self.assertIn("From: id 7\n", c.bot.send_message.await_args.args[1])

    async def test_bare_command_then_next_message(self):
        u, c = fake_update(7, "/feedback"), fake_context()
        await bot.cmd_feedback(u, c)
        self.assertIn("next message", u.message.reply_text.await_args.args[0])
        self.assertEqual(await self.stored(), [])
        await bot.on_text(fake_update(7, "please add PM10"), c)
        self.assertEqual((await self.stored())[0]["text"], "please add PM10")
        await bot.on_text(fake_update(7, "just chatting"), c)  # prompt used up
        self.assertEqual(len(await self.stored()), 1)

    async def test_text_without_prompt_is_ignored(self):
        u = fake_update(7, "hello bot")
        await bot.on_text(u, fake_context())
        self.assertEqual(await self.stored(), [])
        u.message.reply_text.assert_not_awaited()

    async def test_prompt_expires(self):
        await bot.cmd_feedback(fake_update(7, "/feedback"), fake_context())
        bot._awaiting_feedback["7"] -= feedback.AWAIT_SECS + 1
        await bot.on_text(fake_update(7, "too late"), fake_context())
        self.assertEqual(await self.stored(), [])

    async def test_cancelled_prompt_ignores_next_text(self):
        await bot.cmd_feedback(fake_update(7, "/feedback"), fake_context())
        await bot._cancel_feedback_prompt(fake_update(7, "/psi"), fake_context())
        await bot.on_text(fake_update(7, "hello"), fake_context())
        self.assertEqual(await self.stored(), [])

    async def test_prompt_is_per_user(self):
        await bot.cmd_feedback(fake_update(7, "/feedback"), fake_context())
        await bot.on_text(fake_update(8, "not mine"), fake_context())
        self.assertEqual(await self.stored(), [])

    async def test_group_redirects_and_saves_nothing(self):
        u = fake_update(7, "/feedback hi", chat_type=ChatType.GROUP)
        await bot.cmd_feedback(u, fake_context())
        self.assertIn("private chat", u.message.reply_text.await_args.args[0])
        self.assertEqual(await self.stored(), [])

    async def test_truncates_long_feedback(self):
        u = fake_update(7, "/feedback " + "x" * 1500)
        await bot.cmd_feedback(u, fake_context())
        self.assertEqual(len((await self.stored())[0]["text"]), feedback.MAX_LEN)
        self.assertIn("trimmed to 1,000 characters", u.message.reply_text.await_args.args[0])

    async def test_admin_dm_failure_still_saves_and_thanks(self):
        u, c = fake_update(7, "/feedback hi"), fake_context()
        c.bot.send_message.side_effect = RuntimeError
        with self.assertLogs("bot", level="WARNING"):
            await bot.cmd_feedback(u, c)
        self.assertEqual(len(await self.stored()), 1)
        self.assertIn("Thanks", u.message.reply_text.await_args.args[0])

    async def test_redis_failure_tells_user(self):
        self.redis.fail = True
        u, c = fake_update(7, "/feedback hi"), fake_context()
        with self.assertLogs("bot", level="WARNING"):
            await bot.cmd_feedback(u, c)
        self.assertIn("try again", u.message.reply_text.await_args.args[0])
        c.bot.send_message.assert_not_awaited()

    async def test_stored_list_is_capped(self):
        with patch.object(feedback, "MAX_STORED", 3):
            for i in range(5):
                await feedback.save(7, "jane", f"msg {i}")
        self.assertEqual([e["text"] for e in await self.stored()], ["msg 4", "msg 3", "msg 2"])


class RateLimitTest(FeedbackTestCase):
    async def _send(self, uid, text):
        u = fake_update(uid, f"/feedback {text}")
        await bot.cmd_feedback(u, fake_context())
        return u.message.reply_text.await_args.args[0]

    async def test_five_per_minute_then_refused(self):
        for i in range(5):
            self.assertIn("Thanks", await self._send(7, f"m{i}"))
        self.assertIn("please wait a minute", await self._send(7, "m5"))
        self.assertEqual(len(await self.stored()), 5)

    async def test_window_slides(self):
        for i in range(5):
            await self._send(7, f"m{i}")
        bot._feedback_times["7"][0] -= feedback.RATE_WINDOW_SECS + 1  # oldest leaves the window
        self.assertIn("Thanks", await self._send(7, "m5"))
        self.assertIn("please wait", await self._send(7, "m6"))

    async def test_limit_is_per_user(self):
        for i in range(5):
            await self._send(7, f"m{i}")
        self.assertIn("Thanks", await self._send(8, "other user"))


class AdminListTest(FeedbackTestCase):
    async def _list(self, uid=ADMIN, chat_type=ChatType.PRIVATE, args=None):
        u = fake_update(uid, "/feedback_list", chat_type=chat_type)
        await bot.cmd_feedback_list(u, fake_context(args))
        return [call.args[0] for call in u.message.reply_text.await_args_list]

    async def test_only_admin_in_private_chat(self):
        await feedback.save(7, "jane", "hi")
        for uid, chat_type in [(7, ChatType.PRIVATE), (ADMIN, ChatType.GROUP),
                               (ADMIN, ChatType.SUPERGROUP)]:
            self.assertEqual(await self._list(uid, chat_type), [], (uid, chat_type))
        (reply,) = await self._list()
        self.assertIn("newest 1 of 1", reply)
        self.assertIn("From: @jane · id 7", reply)

    async def test_empty(self):
        self.assertEqual(await self._list(), ["No feedback yet."])

    async def test_newest_first_default_10_and_n(self):
        for i in range(15):
            await feedback.save(7, "jane", f"msg {i}")
        (reply,) = await self._list()
        self.assertIn("newest 10 of 15", reply)
        self.assertLess(reply.index("msg 14"), reply.index("msg 13"))
        self.assertNotIn("msg 4\n", reply + "\n")
        (reply,) = await self._list(args=["3"])
        self.assertIn("newest 3 of 15", reply)
        (reply,) = await self._list(args=["junk"])
        self.assertIn("newest 10 of 15", reply)

    async def test_max_50_and_split_under_telegram_limit(self):
        for i in range(60):
            await feedback.save(7, "jane", f"{i:02d} " + "y" * 900)
        replies = await self._list(args=["500"])
        self.assertGreater(len(replies), 1)
        self.assertTrue(all(len(r) <= 4096 for r in replies))
        joined = "".join(replies)
        self.assertIn("newest 50 of 60", joined)
        self.assertEqual(sum(joined.count(f"{i:02d} y") for i in range(60)), 50)

    async def test_html_is_escaped(self):
        c = fake_context()
        await bot.cmd_feedback(fake_update(7, "/feedback <b>bold</b> & <a href='x'>", username="a<b>"), c)
        dm = c.bot.send_message.await_args.args[1]
        self.assertEqual(c.bot.send_message.await_args.kwargs["parse_mode"], ParseMode.HTML)
        (listing,) = await self._list()
        for text in (dm, listing):
            self.assertIn("&lt;b&gt;bold&lt;/b&gt; &amp; &lt;a href=&#x27;x&#x27;&gt;", text)
            self.assertIn("@a&lt;b&gt;", text)


class PostInitTest(unittest.IsolatedAsyncioTestCase):
    def _app(self, short, desc):
        return SimpleNamespace(bot=SimpleNamespace(
            delete_my_commands=AsyncMock(), set_my_commands=AsyncMock(),
            get_my_short_description=AsyncMock(return_value=SimpleNamespace(short_description=short)),
            get_my_description=AsyncMock(return_value=SimpleNamespace(description=desc)),
            set_my_short_description=AsyncMock(), set_my_description=AsyncMock(),
        ))

    async def test_menu_lists_feedback_but_not_admin_commands(self):
        app = self._app(bot.SHORT_DESCRIPTION, bot.DESCRIPTION)
        await bot._post_init(app)
        names = [c.command for c in app.bot.set_my_commands.await_args.args[0]]
        self.assertIn("feedback", names)
        self.assertNotIn("feedback_list", names)
        self.assertNotIn("stats", names)

    async def test_profile_texts_written_only_when_changed(self):
        app = self._app(bot.SHORT_DESCRIPTION, bot.DESCRIPTION)
        await bot._post_init(app)
        app.bot.set_my_short_description.assert_not_awaited()
        app.bot.set_my_description.assert_not_awaited()

        app = self._app("old about", "old intro")
        await bot._post_init(app)
        app.bot.set_my_short_description.assert_awaited_once_with(bot.SHORT_DESCRIPTION)
        app.bot.set_my_description.assert_awaited_once_with(bot.DESCRIPTION)


class RoutingTest(unittest.TestCase):
    """Handler order in the real app: the PSI button and commands beat feedback capture."""

    def setUp(self):
        captured = {}
        with patch.object(Application, "run_webhook", lambda self, **kw: captured.update(app=self)):
            bot.main()
        self.app = captured["app"]

    def handler_for(self, text, chat_type="private"):
        """First matching non-command handler (command matching needs a logged-in bot)."""
        upd = TgUpdate.de_json({"update_id": 1, "message": {
            "message_id": 1, "date": 0, "text": text,
            "chat": {"id": 5, "type": chat_type}, "from": {"id": 5, "is_bot": False, "first_name": "u"},
        }}, self.app.bot)
        return next((h.callback for h in self.app.handlers[0]
                     if not isinstance(h, CommandHandler) and h.check_update(upd)), None)

    def test_text_routing(self):
        self.assertIs(self.handler_for(bot.PSI_BUTTON), bot.cmd_psi)  # never captured as feedback
        self.assertIs(self.handler_for("some text"), bot.on_text)
        self.assertIsNone(self.handler_for("some text", "group"))

    def test_text_capture_skips_commands(self):
        upd = TgUpdate.de_json({"update_id": 1, "message": {
            "message_id": 1, "date": 0, "text": "/psi",
            "entities": [{"type": "bot_command", "offset": 0, "length": 4}],
            "chat": {"id": 5, "type": "private"}, "from": {"id": 5, "is_bot": False, "first_name": "u"},
        }}, self.app.bot)
        (capture,) = [h for h in self.app.handlers[0] if h.callback is bot.on_text]
        self.assertFalse(capture.check_update(upd))

    def test_other_actions_cancel_a_pending_prompt(self):
        def update(**msg):
            return TgUpdate.de_json({"update_id": 1, "message": {
                "message_id": 1, "date": 0, "chat": {"id": 5, "type": "private"},
                "from": {"id": 5, "is_bot": False, "first_name": "u"}, **msg}}, self.app.bot)
        (cancel,) = self.app.handlers[-1]  # runs before the regular handlers
        self.assertIs(cancel.callback, bot._cancel_feedback_prompt)
        command = update(text="/psi", entities=[{"type": "bot_command", "offset": 0, "length": 4}])
        for upd in (command, update(text=bot.PSI_BUTTON),
                    update(location={"latitude": 1.3, "longitude": 103.9})):
            self.assertTrue(cancel.check_update(upd))
        self.assertFalse(cancel.check_update(update(text="my feedback")))

    def test_commands_registered(self):
        commands = {next(iter(h.commands)): h.callback
                    for h in self.app.handlers[0] if isinstance(h, CommandHandler)}
        self.assertIs(commands["feedback"], bot.cmd_feedback)
        self.assertIs(commands["feedback_list"], bot.cmd_feedback_list)

    def test_profile_texts_fit_and_mention_feedback(self):
        self.assertLessEqual(len(bot.SHORT_DESCRIPTION), 120)  # Telegram's limits
        self.assertLessEqual(len(bot.DESCRIPTION), 512)
        for text in (bot.SHORT_DESCRIPTION, bot.DESCRIPTION):
            self.assertIn("/feedback", text)
            self.assertNotIn("/feedback_list", text)
            self.assertNotIn("**", text)  # shown literally, not as bold

    def test_menu_and_help(self):
        self.assertIn("/feedback —", bot.COMMANDS_TEXT)
        self.assertNotIn("/feedback_list", bot.COMMANDS_TEXT)


if __name__ == "__main__":
    unittest.main()
