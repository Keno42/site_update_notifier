"""src/lesson.py のテスト. 実行: python -m unittest discover -s tests -t ."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import discord

from src.lesson import (
    LLA_DIR,
    LessonConfig,
    Lessons,
    ReviewSession,
    ReviewView,
    cleanup,
    load_pending,
    pending_from_plan,
    save_pending,
    setup,
)

QUESTIONS = [
    {"items": ["takk"], "prompt": "お礼を言って", "answer": "Takk."},
    {
        "items": ["eg_vil", "fara_heim"],
        "prompt": "帰りたい",
        "answer": "Ég vil fara heim.",
    },
    {"items": ["bless"], "prompt": "さようなら", "answer": "Bless."},
]


class ConfigTests(unittest.TestCase):
    def test_disabled_without_root_or_users(self):
        self.assertIsNone(LessonConfig.from_module(SimpleNamespace()))
        self.assertIsNone(LessonConfig.from_module(SimpleNamespace(LESSON_ROOT="/x")))

    def test_defaults_and_args(self):
        cfg = LessonConfig.from_module(
            SimpleNamespace(
                LESSON_ROOT="/data", LESSON_USERS={1: "yuki"}, LESSON_MINUTES=15
            )
        )
        assert cfg is not None
        self.assertEqual(cfg.users, {1: "yuki"})
        args = cfg.generate_args("yuki")
        self.assertEqual(args[0], "generate")
        self.assertIn("/data/yuki/learner.json", args)
        self.assertIn("/data/yuki/work", args)
        self.assertEqual(args[args.index("--minutes") + 1], "15")
        self.assertNotIn("--user", args, "no settings.json: settings come from config")
        self.assertEqual(
            cfg.report_args("yuki", 6, ["a", "b"]),
            ["report", "--learner", "/data/yuki/learner.json", "--lesson", "6"]
            + ["--failed", "a,b"],
        )
        self.assertNotIn("--failed", cfg.report_args("yuki", 6, []))
        with self.assertRaises(ValueError):
            cfg.user_dir("../x")


class ReviewTests(unittest.TestCase):
    def test_session_collects_failed_and_shaky(self):
        s = ReviewSession(5, QUESTIONS)
        self.assertIn("||Takk.||", s.render(), "the answer is a spoiler")
        s.rate("failed")
        s.rate("shaky")
        self.assertFalse(s.done)
        s.rate("ok")
        self.assertTrue(s.done)
        s.rate("failed")  # a late tap changes nothing
        self.assertEqual(s.failed_ids(), ["takk"])
        self.assertEqual(s.shaky_ids(), ["eg_vil", "fara_heim"])
        summary = s.summary()
        self.assertIn("言えなかった: Takk.", summary)
        self.assertIn("迷った: Ég vil fara heim.", summary)

    def test_limit_prefers_questions_with_new_items_and_keeps_order(self):
        plan = {
            "lesson_number": 3,
            "new_items": [{"id": "bless"}, {"id": "fara_heim"}],
            "review": QUESTIONS,
        }
        self.assertEqual(pending_from_plan(plan)["questions"], QUESTIONS)
        limited = pending_from_plan(plan, limit=2)["questions"]
        self.assertEqual(
            [q["answer"] for q in limited], ["Ég vil fara heim.", "Bless."]
        )

    def test_pending_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pending_review.json"
            save_pending(path, {"lesson": 2, "questions": QUESTIONS})
            self.assertEqual(load_pending(path)["lesson"], 2)
            save_pending(path, {"lesson": 3, "questions": []})
            self.assertFalse(path.exists(), "nothing to review: no file")
            self.assertIsNone(load_pending(path))

    def test_cleanup_keeps_only_the_cache_when_asked(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            (work / "cache" / "edge").mkdir(parents=True)
            (work / "lesson-001.mp3").write_bytes(b"x")
            cleanup(work, keep_cache=True)
            self.assertEqual([p.name for p in work.iterdir()], ["cache"])
            cleanup(work, keep_cache=False)
            self.assertEqual(list(work.iterdir()), [])


class FakeInteraction:
    def __init__(self, user_id):
        self.user = SimpleNamespace(id=user_id)
        self.edits = []
        self.messages = []
        interaction = self

        class Response:
            async def edit_message(self, content=None, view="unchanged"):
                interaction.edits.append((content, view))

            async def send_message(self, content, ephemeral=False):
                interaction.messages.append((content, ephemeral))

        self.response = Response()


class ViewTests(unittest.TestCase):
    def run_view(self, taps):
        """taps: [(user_id, button label)] → (finish calls, interactions)."""

        async def scenario():
            finished = []

            async def finish(reviewed):
                finished.append(reviewed)

            async def expire():
                finished.append("expired")

            view = ReviewView(ReviewSession(4, QUESTIONS), 1, finish, expire)
            buttons = {b.label: b for b in view.children}
            done = []
            for user_id, label in taps:
                it = FakeInteraction(user_id)
                if await view.interaction_check(it):
                    await buttons[label].callback(it)
                done.append(it)
            return finished, done

        return asyncio.run(scenario())

    def test_rating_every_question_finishes_once(self):
        taps = [(1, "言えなかった"), (1, "言えた"), (1, "迷った"), (1, "言えた")]
        finished, its = self.run_view(taps)
        self.assertEqual(finished, [True])
        self.assertIn("||Ég vil fara heim.||", its[0].edits[0][0], "next question")
        self.assertIn("振り返り完了", its[2].edits[0][0])
        self.assertIsNone(its[2].edits[0][1], "buttons removed at the end")
        self.assertEqual(its[3].edits, [], "a tap after the end is ignored")

    def test_only_the_owner_can_answer_and_skip_generates_without_report(self):
        finished, its = self.run_view([(2, "言えた"), (1, "振り返らずに生成")])
        self.assertEqual(its[0].messages, [("本人だけが回答できます。", True)])
        self.assertEqual(finished, [False])

    def test_setup_registers_only_when_configured(self):
        client = discord.Client(intents=discord.Intents.default())
        self.assertIsNone(setup(client, SimpleNamespace()))
        config = SimpleNamespace(LESSON_ROOT="/data", LESSON_USERS={1: "yuki"})
        self.assertTrue(callable(setup(client, config)))


class FakeChannel:
    def __init__(self):
        self.sent = []

    def typing(self):
        channel = self

        class Typing:
            async def __aenter__(self):
                return channel

            async def __aexit__(self, *exc):
                return False

        return Typing()

    async def send(self, content=None, files=None):
        self.sent.append((content, [f.filename for f in files or []]))
        for f in files or []:
            f.close()  # as discord.py does after sending


@unittest.skipUnless((LLA_DIR / "audiolesson").exists(), "submodule not checked out")
class EndToEndTests(unittest.TestCase):
    """実際の CLI (stub 音声) で 生成 → 投稿 → 振り返り → report → 次の生成."""

    def test_two_lessons(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = LessonConfig(
                root=Path(td),
                users={1: "yuki"},
                minutes=3,
                extra_args=["--provider", "stub"],
            )
            lessons = Lessons(cfg)
            channel = FakeChannel()
            asyncio.run(lessons.generate_and_post(channel, "yuki"))
            text, files = channel.sent[-1]
            self.assertIn("レッスン 1", text)
            self.assertIn("lesson-001.wav", files)
            self.assertIn("lesson-001.transcript.md", files)
            user = Path(td) / "yuki"
            kept = sorted(p.name for p in user.iterdir())
            self.assertEqual(kept[:1], ["learner.json"])
            self.assertEqual(list((user / "work").iterdir()), [], "outputs removed")
            pending = load_pending(cfg.pending_path("yuki"))
            if pending is None:
                self.skipTest("submodule predates plan.json `review`")
            session = ReviewSession(pending["lesson"], pending["questions"])
            session.rate("failed")
            while not session.done:
                session.rate("ok")
            self.assertTrue(asyncio.run(lessons.report(channel, "yuki", session)))
            self.assertIsNone(load_pending(cfg.pending_path("yuki")))
            learner = json.loads((user / "learner.json").read_text("utf-8"))
            failed = session.failed_ids()[0]
            self.assertEqual(learner["items"][failed]["failures"], 1)
            asyncio.run(lessons.generate_and_post(channel, "yuki"))
            self.assertIn("レッスン 2", channel.sent[-1][0])


if __name__ == "__main__":
    unittest.main()
