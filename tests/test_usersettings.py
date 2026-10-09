"""#92: per-user lesson options (/lesson-configure): length, when the new list comes, order."""

import asyncio
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src import feedback, newlist, usersettings
from src.lesson import LessonConfig, Lessons
from src.review import review_note
from src.review_queue import ReviewQueue
from tests.test_feedback import saved
from tests.test_lesson import FakeChannel
from tests.test_newlist import PLAN, Env
from tests.test_review_queue import D, entry

US = usersettings.UserSettings


def cfg_in(td, **kw):
    return LessonConfig(root=Path(td), users={1: "yuki"}, **kw)


class SettingsFileTests(unittest.TestCase):
    def test_a_new_user_gets_the_default_and_a_user_with_history_keeps_the_old_behaviour(
        self,
    ):
        with tempfile.TemporaryDirectory() as td:
            cfg = cfg_in(td, minutes=20)
            d = cfg.user_dir("yuki")
            self.assertEqual(cfg.user_settings("yuki"), US(5, "before", "new-first"))
            d.mkdir(parents=True)
            (d / "learner.json").write_text("{}", "utf-8")
            self.assertEqual(cfg.user_settings("yuki"), US(20, "after", "spread"))

    def test_save_and_load_round_trip_and_do_not_touch_the_cli_settings_file(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "u"
            usersettings.save(d, US(15, "after", "new-first"))
            self.assertEqual(
                usersettings.load(
                    d, US(5, "before", "spread"), US(30, "after", "spread")
                ),
                US(15, "after", "new-first"),
            )
            self.assertEqual(usersettings.SETTINGS_FILE, "lesson_settings.json")
            self.assertFalse((d / "settings.json").exists())

    def test_a_broken_file_or_value_falls_back_to_the_default(self):
        default, legacy = US(5, "before", "new-first"), US(30, "after", "spread")
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / usersettings.SETTINGS_FILE).write_text("{not json", "utf-8")
            self.assertEqual(usersettings.load(d, default, legacy), default)
            (d / usersettings.SETTINGS_FILE).write_text("[1]", "utf-8")
            self.assertEqual(usersettings.load(d, default, legacy), default)
            (d / usersettings.SETTINGS_FILE).write_text(
                json.dumps({"minutes": 7, "new_list": "after", "order": "x"}), "utf-8"
            )
            self.assertEqual(
                usersettings.load(d, default, legacy), US(5, "after", "new-first")
            )

    def test_the_review_scales_with_the_length_and_is_the_config_at_30(self):
        full = usersettings.ReviewSize.for_minutes(30, 20, 3, 3)
        self.assertEqual(
            (full.questions, full.scene_cards, full.reading_cards, full.open_limit),
            (20, 3, 3, None),
        )
        short = usersettings.ReviewSize.for_minutes(5, 20, 3, 3)
        self.assertEqual(
            (short.questions, short.scene_cards, short.reading_cards), (4, 1, 1)
        )
        self.assertEqual(short.open_limit, 1)
        self.assertEqual(usersettings.scaled(0, 5), 0)  # no limit stays no limit


class ConfigureTests(unittest.TestCase):
    def interaction(self, replies, user=1):
        class Response:
            async def send_message(self, content, ephemeral=False, view=None):
                replies.append((content, ephemeral))

        return SimpleNamespace(user=SimpleNamespace(id=user), response=Response())

    def test_configure_changes_only_what_is_given_and_answers_privately(self):
        with tempfile.TemporaryDirectory() as td:
            lessons = Lessons(cfg_in(td))
            replies = []
            asyncio.run(lessons.configure(self.interaction(replies), order="spread"))
            asyncio.run(lessons.configure(self.interaction(replies), minutes=10))
            self.assertEqual(
                lessons.cfg.user_settings("yuki"), US(10, "before", "spread")
            )
            asyncio.run(lessons.configure(self.interaction(replies)))
            self.assertTrue(all(private for _, private in replies))
            self.assertIn("今の設定です", replies[-1][0])
            self.assertIn("10分", replies[-1][0])

    def test_an_unregistered_user_cannot_configure(self):
        with tempfile.TemporaryDirectory() as td:
            lessons = Lessons(cfg_in(td))
            replies = []
            asyncio.run(lessons.configure(self.interaction(replies, user=9), minutes=5))
            self.assertIn("登録", replies[0][0])
            self.assertFalse(lessons.cfg.user_dir("yuki").exists())


class GenerateTests(unittest.TestCase):
    def test_minutes_and_order_reach_the_cli_and_spread_is_not_passed(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = cfg_in(td, minutes=30)
            args = cfg.generate_args("yuki", minutes=10, order="new-first")
            self.assertEqual(args[args.index("--minutes") + 1], "10")
            self.assertEqual(args[args.index("--order") + 1], "new-first")
            old = cfg.generate_args("yuki", order="spread")
            self.assertNotIn("--order", old)
            self.assertEqual(old[old.index("--minutes") + 1], "30")

    def test_lesson_minutes_is_one_time_and_does_not_change_the_stored_default(self):
        with tempfile.TemporaryDirectory() as td:
            lessons = Lessons(cfg_in(td, minutes=20), today=lambda: D)
            usersettings.save(lessons.cfg.user_dir("yuki"), US(5, "after", "spread"))
            channel = FakeChannel()
            replies = []

            class Response:
                async def send_message(self, content, ephemeral=False, view=None):
                    replies.append(content)

            inter = SimpleNamespace(
                user=SimpleNamespace(id=1),
                channel_id=0,
                channel=channel,
                response=Response(),
            )
            with mock.patch.object(
                Lessons, "generate_and_post", new=mock.AsyncMock()
            ) as gen:
                asyncio.run(lessons.start(inter, minutes=30))
            self.assertEqual(gen.await_args.kwargs["minutes"], 30)
            self.assertEqual(lessons.cfg.user_settings("yuki").minutes, 5)

    def test_the_settings_reach_the_cli_when_generating(self):
        with tempfile.TemporaryDirectory() as td:
            lessons = Lessons(cfg_in(td, minutes=20))
            usersettings.save(
                lessons.cfg.user_dir("yuki"), US(10, "after", "new-first")
            )
            seen = []

            async def fake_cli(cfg, args, on_progress=None):
                seen.append(args)
                return 1, "", "stop"

            with mock.patch("src.lesson.run_cli", fake_cli):
                asyncio.run(lessons.generate_and_post(FakeChannel(), "yuki"))
                asyncio.run(
                    lessons.generate_and_post(FakeChannel(), "yuki", minutes=30)
                )
            first, second = seen
            self.assertEqual(first[first.index("--minutes") + 1], "10")
            self.assertEqual(first[first.index("--order") + 1], "new-first")
            self.assertEqual(second[second.index("--minutes") + 1], "30")


class ReviewSizeTests(unittest.TestCase):
    def queue(self, n_open):
        q = ReviewQueue()
        for n in range(n_open):
            e = entry(state="seen")
            e.open = True
            e.source_lesson = n + 1
            q.entries[f"o{n}"] = e
        return q

    def test_a_short_lesson_limits_the_required_open_checks_oldest_first(self):
        q = self.queue(8)
        self.assertEqual(len(q.must_answer(D)), 8)
        self.assertEqual(q.must_answer(D, open_limit=2), ["o0", "o1"])
        self.assertEqual(len(q.select(D, 4, open_limit=1)), 4)

    def test_the_note_counts_what_will_be_asked(self):
        q = self.queue(8)
        self.assertEqual(review_note(q, D, 4, open_limit=2), review_note(q, D, 4, 2))
        self.assertIn("次回 8問", review_note(q, D, 4))
        self.assertIn("次回 4問", review_note(q, D, 4, open_limit=2))


class NewListTimingTests(unittest.TestCase):
    def test_before_posts_the_list_right_after_the_lesson_and_keeps_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            env = Env(td)
            asyncio.run(
                env.lessons.post(
                    env.channel,
                    env.work,
                    PLAN,
                    "",
                    1,
                    env.manifest,
                    [],
                    new_list="before",
                )
            )
            self.assertEqual(len(env.channel.sent), 2)
            self.assertIn("新出表現", env.channel.sent[1][0])
            self.assertEqual(env.pending(), {})
            self.assertFalse(
                any(
                    "newlist" in (getattr(c, "custom_id", "") or "")
                    for c in env.channel.views[0].children
                )
            )

    def test_after_keeps_the_list_and_offers_the_show_now_button(self):
        with tempfile.TemporaryDirectory() as td:
            env = Env(td)
            env.post()
            self.assertEqual(len(env.channel.sent), 1)
            self.assertEqual(len(env.pending()), 1)
            buttons = [
                c
                for c in env.channel.views[0].children
                if "newlist" in (c.custom_id or "")
            ]
            self.assertEqual(len(buttons), 1)

    def test_the_button_shows_the_list_once_and_only_to_the_owner(self):
        with tempfile.TemporaryDirectory() as td:
            env = Env(td)
            env.post()
            newlist.NewListButton.users = env.cfg.users
            newlist.NewListButton.user_dir = env.cfg.user_dir
            button = newlist.NewListButton(1, env.manifest)
            sent = []

            def inter(uid):
                async def send_message(content, ephemeral=False):
                    sent.append((content, ephemeral))

                return SimpleNamespace(
                    user=SimpleNamespace(id=uid),
                    response=SimpleNamespace(send_message=send_message),
                )

            asyncio.run(button.callback(inter(2)))
            self.assertTrue(sent[-1][1])
            self.assertEqual(len(env.pending()), 1)
            asyncio.run(button.callback(inter(1)))
            self.assertIn("miða", sent[-1][0])
            self.assertEqual(env.pending(), {})
            asyncio.run(button.callback(inter(1)))
            self.assertIn("もう出て", sent[-1][0])


class CompactFeedbackTests(unittest.TestCase):
    def test_a_short_lesson_asks_only_load_and_sooner_and_hides_the_candidates(self):
        with tempfile.TemporaryDirectory() as td:
            record = saved(Path(td)).load()
            assert record is not None
            self.assertEqual(feedback.lesson_minutes(record), usersettings.BASE_MINUTES)
            record.plan["config"] = {"minutes": 5}
            self.assertTrue(
                feedback.lesson_minutes(record) < usersettings.COMPACT_FEEDBACK_BELOW
            )

            async def build():
                return (
                    feedback.FeedbackView(record, 1, None, compact=True),  # type: ignore[arg-type]
                    feedback.FeedbackView(record, 1, None),  # type: ignore[arg-type]
                )

            view, full = asyncio.run(build())
            labels = [getattr(c, "label", None) for c in view.children]
            self.assertIn("送信", labels)
            self.assertNotIn("メモを書く", labels)
            self.assertFalse(hasattr(view, "concerns"))
            self.assertNotIn(
                "機械が気づいたこと", feedback.form_text(record, compact=True)
            )
            self.assertIn(
                "メモを書く", [getattr(c, "label", None) for c in full.children]
            )


if __name__ == "__main__":
    unittest.main()
