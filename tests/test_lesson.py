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
    StatusMessage,
    cleanup,
    load_pending,
    pending_from_plan,
    run_cli,
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
        self.assertNotIn("--auto", args)
        self.assertEqual(cfg.generate_args("yuki", auto=True).count("--auto"), 1)
        cfg.extra_args = ["--auto"]
        self.assertEqual(cfg.generate_args("yuki", auto=True).count("--auto"), 1)
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

    def test_cleanup_empties_the_work_dir(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            (work / "cache" / "edge").mkdir(parents=True)
            (work / "lesson-001.mp3").write_bytes(b"x")
            cleanup(work)
            self.assertEqual(list(work.iterdir()), [])

    def test_cache_is_shared_only_when_kept(self):
        cfg = LessonConfig(root=Path("/data"), users={1: "a", 2: "b"})
        self.assertEqual(cfg.cache_dir("a"), Path("/data/a/work/cache"))
        cfg.keep_cache = True
        self.assertEqual(cfg.cache_dir("a"), cfg.cache_dir("b"))
        self.assertEqual(cfg.cache_dir("a"), Path("/data/tts-cache"))
        args = cfg.generate_args("a")
        self.assertEqual(args[args.index("--cache") + 1], "/data/tts-cache")


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


FAKE_CLI = """
import sys, time
for i in range(10, 31, 10):
    print(f"  synthesized {i}/30 new lines", file=sys.stderr, end="\\r", flush=True)
    time.sleep(0.05)
time.sleep(float(sys.argv[1]))
print("done")
"""


class ProgressTests(unittest.TestCase):
    def fake_cfg(self, td, timeout_min=1.0):
        pkg = Path(td) / "audiolesson"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "cli.py").write_text(FAKE_CLI)
        return LessonConfig(
            root=Path(td), users={}, lla_dir=Path(td), timeout_min=timeout_min
        )

    def test_progress_lines_are_streamed(self):
        with tempfile.TemporaryDirectory() as td:
            seen = []

            async def on_progress(line):
                seen.append(line)

            rc, out, err = asyncio.run(run_cli(self.fake_cfg(td), ["0"], on_progress))
            self.assertEqual((rc, out.strip()), (0, "done"))
            self.assertIn("synthesized 30/30 new lines", seen)
            self.assertIn("synthesized 30/30", err)

    def test_timeout_kills_the_process(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.fake_cfg(td, timeout_min=0.01)
            rc, _, err = asyncio.run(run_cli(cfg, ["30"]))
            self.assertEqual(rc, -1)
            self.assertIn("終わらなかった", err)

    def test_status_message_shows_synthesis_progress(self):
        edits = []

        class Message:
            async def edit(self, content):
                edits.append(content)

        class Channel:
            async def send(self, content):
                edits.append(content)
                return Message()

        async def scenario():
            status = StatusMessage(Channel(), interval=0)
            await status.start()
            await status.update("  synthesized 120/450 new lines")
            await status.finish(False)

        asyncio.run(scenario())
        self.assertTrue(edits[0].startswith("生成中…"))
        self.assertIn("音声合成 120/450", edits[1])
        self.assertTrue(edits[2].startswith("生成できませんでした"))


class FakeChannel(discord.abc.Messageable):
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

    def test_second_learner_reuses_the_shared_cache(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = LessonConfig(
                root=Path(td),
                users={1: "a", 2: "b"},
                minutes=3,
                extra_args=["--provider", "stub", "--date", "2026-09-18"],
                keep_cache=True,
            )
            lessons = Lessons(cfg)
            channel = FakeChannel()
            asyncio.run(lessons.generate_and_post(channel, "a"))
            clips = sorted((Path(td) / "tts-cache").iterdir())
            self.assertTrue(clips)
            asyncio.run(lessons.generate_and_post(channel, "b"))
            self.assertEqual(sorted((Path(td) / "tts-cache").iterdir()), clips)
            self.assertEqual(list(cfg.work_dir("a").iterdir()), [])

    def test_auto_skips_review_and_self_report(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = LessonConfig(
                root=Path(td),
                users={1: "yuki"},
                minutes=3,
                extra_args=["--provider", "stub"],
            )
            lessons = Lessons(cfg)
            channel = FakeChannel()
            cfg.user_dir("yuki").mkdir(parents=True)
            stale = {"lesson": 9, "questions": QUESTIONS}
            save_pending(cfg.pending_path("yuki"), stale)
            replies = []

            class Response:
                async def send_message(self, content, ephemeral=False, view=None):
                    replies.append((content, view))

            interaction = SimpleNamespace(
                user=SimpleNamespace(id=1),
                channel_id=5,
                channel=channel,
                response=Response(),
            )
            asyncio.run(lessons.start(interaction, auto=True))
            self.assertIn("自動モード", replies[0][0])
            self.assertIsNone(replies[0][1], "no review buttons")
            text, files = channel.sent[-1]
            self.assertIn("レッスン 1", text)
            self.assertNotIn("振り返り", text)
            learner = json.loads(cfg.learner_path("yuki").read_text("utf-8"))
            self.assertEqual(learner["feedback_mode"], "auto")
            pending = load_pending(cfg.pending_path("yuki"))
            self.assertTrue(
                pending is None or pending["lesson"] == 1, "stale review dropped"
            )
            self.assertEqual(lessons.busy, set())


if __name__ == "__main__":
    unittest.main()
