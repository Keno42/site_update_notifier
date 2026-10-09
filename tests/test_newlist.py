"""#79: the new-expression list comes after the lesson has been heard."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src import newlist
from src.feedback import Feedback, Ledger
from src.lesson import LessonConfig, Lessons
from tests.test_feedback import FakeResponse, submit_form, write_lesson
from tests.test_lesson import FakeChannel

NOW = datetime(2026, 10, 8, 21, 10, tzinfo=timezone(timedelta(hours=9)))
PLAN = {
    "lesson_number": 19,
    "summary": {"duration_s": 1740},
    "new_items": [
        {"id": "mida", "target": "miða", "meaning": "ticket"},
        {"id": "sundfot", "target": "sundföt", "meaning": "swimwear"},
        {
            "id": "mida",
            "target": "miða",
            "meaning": "ticket",
        },  # embedded, then introduced
        {"id": "bare", "target": "Já.", "meaning": None},
    ],
    "reviewed_items": [],
    "review_candidates": [],
}


class Env:
    """A lesson posted for yuki (Discord id 1), with its record saved."""

    def __init__(self, td):
        self.td = Path(td)
        self.cfg = LessonConfig(
            root=self.td,
            users={1: "yuki"},
            minutes=3,
            default_minutes=3,
            default_new_list="after",
            default_order="spread",
        )
        self.lessons = Lessons(self.cfg)
        self.channel = FakeChannel()
        work = self.td / "work"
        write_lesson(work, PLAN)
        user = self.cfg.user_dir("yuki")
        user.mkdir(parents=True, exist_ok=True)
        learner = user / "learner.json"
        learner.write_text("{}", "utf-8")
        self.manifest = Ledger(user).save_manifest(
            work, PLAN, b"{}", learner, {"bot": "a" * 40, "lla": "b" * 40}, ["generate"], NOW
        ).name  # fmt: skip
        self.work = work

    def post(self):
        asyncio.run(
            self.lessons.post(self.channel, self.work, PLAN, "", 1, self.manifest, [])
        )

    def pending(self):
        return newlist.PendingLists(self.cfg.user_dir("yuki"))._load()


class NewListTests(unittest.TestCase):
    def test_the_post_has_the_count_and_not_the_expressions(self):
        with tempfile.TemporaryDirectory() as td:
            env = Env(td)
            env.post()
            text = env.channel.sent[0][0]
            self.assertIn("新出 3", text, "three distinct items, «miða» once")
            for word in ("miða", "sundföt", "ticket"):
                self.assertNotIn(word, text, "nothing to read before listening")
            self.assertEqual(
                list(env.pending()), [env.manifest], "kept until it is shown"
            )

    def test_sending_the_feedback_replies_to_the_lesson_post_with_the_list(self):
        with tempfile.TemporaryDirectory() as td:
            env = Env(td)
            env.post()
            post_id = 1001  # the id FakeChannel gave the post
            fb = Feedback({1: "yuki"}, env.cfg.user_dir, 0, lambda: NOW)

            async def run():
                log = []
                i = SimpleNamespace(
                    user=SimpleNamespace(id=1), channel_id=5, channel=env.channel,
                    response=FakeResponse(log),
                )  # fmt: skip
                await fb.open_form(i)
                await submit_form(self, log, log[-1][2])

            before = len(env.channel.sent)
            asyncio.run(run())
            self.assertEqual(len(env.channel.sent), before + 1)
            text = env.channel.sent[-1][0]
            self.assertEqual(
                text,
                "**レッスン 19 の新出表現**\n・miða — ticket\n・sundföt — swimwear\n・Já.",
            )
            ref = env.channel.references[-1]
            self.assertEqual((ref.message_id, ref.channel_id), (post_id, 5))
            self.assertFalse(ref.fail_if_not_exists)
            self.assertEqual(env.pending(), {}, "shown once")
            # the feedback is recorded either way
            self.assertEqual(
                len(Ledger(env.cfg.user_dir("yuki")).events(env.manifest)), 1
            )

    def test_a_list_that_cannot_be_posted_does_not_fail_the_feedback(self):
        with tempfile.TemporaryDirectory() as td:
            env = Env(td)
            env.post()
            fb = Feedback({1: "yuki"}, env.cfg.user_dir, 0, lambda: NOW)

            async def broken(*a, **k):
                raise RuntimeError("403")

            env.channel.send = broken

            async def run():
                log = []
                i = SimpleNamespace(
                    user=SimpleNamespace(id=1), channel_id=5, channel=env.channel,
                    response=FakeResponse(log),
                )  # fmt: skip
                await fb.open_form(i)
                with self.assertLogs(level="ERROR"):
                    await submit_form(self, log, log[-1][2])
                return log

            log = asyncio.run(run())
            self.assertEqual(
                len(Ledger(env.cfg.user_dir("yuki")).events(env.manifest)), 1
            )
            self.assertIn("記録しました", log[-1][1], "the learner is told it was recorded")

    def test_without_feedback_the_list_comes_when_the_next_lesson_starts_generating(
        self,
    ):
        with tempfile.TemporaryDirectory() as td:
            env = Env(td)
            env.post()
            before = len(env.channel.sent)
            with mock.patch.object(Lessons, "_generate_and_post", mock.AsyncMock()):
                asyncio.run(env.lessons.generate_and_post(env.channel, "yuki"))
            self.assertEqual(len(env.channel.sent), before + 1)
            self.assertIn("・miða — ticket", env.channel.sent[-1][0])
            self.assertEqual(env.pending(), {})
            with mock.patch.object(Lessons, "_generate_and_post", mock.AsyncMock()):
                asyncio.run(env.lessons.generate_and_post(env.channel, "yuki"))
            self.assertEqual(len(env.channel.sent), before + 1, "never twice")

    def test_a_regenerated_lesson_replaces_the_list_of_the_version_it_supersedes(self):
        """#83 review: lesson 19 generated twice is kept as lesson-019 and lesson-019.2. The feedback for the one heard takes
        its own list; the next generation must not post the superseded version's list as a second «レッスン 19».
        """
        with tempfile.TemporaryDirectory() as td:
            env = Env(td)
            env.post()  # lesson-019
            first = env.manifest
            plan2 = dict(
                PLAN, new_items=[{"id": "other", "target": "annað", "meaning": "other"}]
            )
            write_lesson(env.work, plan2)
            user = env.cfg.user_dir("yuki")
            second = Ledger(user).save_manifest(
                env.work, plan2, b"{}", user / "learner.json", {"bot": "a" * 40, "lla": "b" * 40}, ["generate"], NOW
            ).name  # fmt: skip
            self.assertNotEqual(first, second)
            asyncio.run(
                env.lessons.post(env.channel, env.work, plan2, "", 1, second, [])
            )
            self.assertEqual(
                list(env.pending()), [second], "the superseded list went with its post"
            )
            fb = Feedback({1: "yuki"}, env.cfg.user_dir, 0, lambda: NOW)

            async def run():
                log = []
                i = SimpleNamespace(
                    user=SimpleNamespace(id=1), channel_id=5, channel=env.channel,
                    response=FakeResponse(log),
                )  # fmt: skip
                await fb.open_form(i, second)
                await submit_form(self, log, log[-1][2])

            asyncio.run(run())
            before = len(env.channel.sent)
            with mock.patch.object(Lessons, "_generate_and_post", mock.AsyncMock()):
                asyncio.run(env.lessons.generate_and_post(env.channel, "yuki"))
            self.assertEqual(
                len(env.channel.sent), before, "no second list for lesson 19"
            )
            lists = [t for t, _ in env.channel.sent if "新出表現" in t]
            self.assertEqual(len(lists), 1)
            self.assertIn("annað", lists[0])

    def test_a_list_already_shown_with_the_feedback_is_not_posted_again_later(self):
        with tempfile.TemporaryDirectory() as td:
            env = Env(td)
            env.post()
            newlist.PendingLists(env.cfg.user_dir("yuki")).take(env.manifest)
            before = len(env.channel.sent)
            with mock.patch.object(Lessons, "_generate_and_post", mock.AsyncMock()):
                asyncio.run(env.lessons.generate_and_post(env.channel, "yuki"))
            self.assertEqual(len(env.channel.sent), before)


if __name__ == "__main__":
    unittest.main()
