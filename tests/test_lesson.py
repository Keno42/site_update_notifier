"""src/lesson.py のテスト. 実行: python -m unittest discover -s tests -t ."""

import asyncio
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import hashlib

import discord

from src import feedback

from src.lesson import (
    LLA_DIR,
    LessonConfig,
    Lessons,
    StatusMessage,
    cleanup,
    run_cli,
    setup,
)
from src.review import ReviewSession, ReviewView
from src.review_queue import Entry
from src.review_queue import ReviewQueue

D = date(2026, 9, 26)

QUESTIONS = [
    {"items": ["takk"], "prompt": "お礼を言って", "answer": "Takk."},
    {
        "items": ["eg_vil", "fara_heim"],
        "prompt": "帰りたい",
        "answer": "Ég vil fara heim.",
    },
    {"items": ["bless"], "prompt": "さようなら", "answer": "Bless."},
]


def legacy_file(path, lesson=4):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"lesson": lesson, "questions": QUESTIONS}), "utf-8")


def session_on(td):
    """3 問 (QUESTIONS) が今日期限のキューと、その全問の振り返り."""
    path = Path(td) / "pending_review.json"
    legacy_file(path)
    queue = ReviewQueue.load(path, D)
    return ReviewSession(queue, queue.select(D), path, D)


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
            cfg.report_args("yuki", ["a", "b"]),
            ["report", "--learner", "/data/yuki/learner.json", "--failed", "a,b"],
        )
        self.assertEqual(cfg.report_args("yuki", [], lesson=6)[-2:], ["--lesson", "6"])
        self.assertNotIn("--failed", cfg.report_args("yuki", []))
        self.assertEqual(
            cfg.report_args("yuki", [], lesson=6, hesitated=["c"], recalled=["d", "e"])[
                -4:
            ],
            ["--hesitated", "c", "--recalled", "d,e"],
        )
        self.assertEqual(cfg.review_limit, 20, "a session is bounded by default")
        self.assertNotIn("--auto", args)
        self.assertEqual(cfg.generate_args("yuki", auto=True).count("--auto"), 1)
        cfg.extra_args = ["--auto"]
        self.assertEqual(cfg.generate_args("yuki", auto=True).count("--auto"), 1)
        with self.assertRaises(ValueError):
            cfg.user_dir("../x")


class ReviewTests(unittest.TestCase):
    def test_session_saves_each_answer_to_the_queue(self):
        with tempfile.TemporaryDirectory() as td:
            s = session_on(td)
            self.assertNotIn("Takk.", s.render(), "no answer before the button")
            self.assertNotIn(
                "||", s.render(), "no spoiler: desktop Discord keeps it open"
            )
            self.assertIn("答え: **Takk.**", s.render(revealed=True))
            s.rate("failed")
            on_disk = ReviewQueue.load(s.path, D).entries
            self.assertEqual(on_disk["takk"].state, "failed", "saved right away")
            self.assertEqual(on_disk["bless"].state, "unseen")
            s.rate("shaky")
            self.assertFalse(s.done)
            s.rate("ok")
            self.assertTrue(s.done)
            s.rate("failed")  # a late tap changes nothing
            self.assertEqual(ReviewQueue.load(s.path, D).entries["bless"].state, "ok")
            self.assertEqual(s.failed_ids(), ["takk"])
            self.assertEqual(s.shaky_ids(), ["eg_vil", "fara_heim"])
            summary = s.summary()
            self.assertIn("言えなかった: Takk.", summary)
            self.assertIn("迷った: Ég vil fara heim.", summary)

    def test_unanswered_questions_stay_in_the_queue(self):
        with tempfile.TemporaryDirectory() as td:
            s = session_on(td)
            s.rate("ok")
            self.assertIn("未回答の 2 問は次回", s.summary())
            states = {
                k: e.state for k, e in ReviewQueue.load(s.path, D).entries.items()
            }
            self.assertEqual(sorted(states.values()), ["ok", "unseen", "unseen"])

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
    def run_view(self, taps, timeout=False):
        """taps: [(user_id, button label)] → (finish calls, interactions, session)."""

        async def scenario(td):
            finished = []

            async def finish(generate):
                finished.append(generate)

            async def expire():
                finished.append("expired")

            session = session_on(td)
            view = ReviewView(session, 1, finish, expire)
            done = []
            for user_id, label in taps:
                it = FakeInteraction(user_id)
                buttons = {b.label: b for b in view.children}  # they change per step
                it.labels = sorted(buttons)
                if await view.interaction_check(it):
                    if label in buttons:
                        await buttons[label].callback(it)
                done.append(it)
            if timeout:
                await view.on_timeout()
            session.saved = sorted(  # what is on disk, read before the temp dir goes
                e.state for e in ReviewQueue.load(session.path, D).entries.values()
            )
            return finished, done, session

        with tempfile.TemporaryDirectory() as td:
            return asyncio.run(scenario(td))

    def test_rating_every_question_finishes_once(self):
        see = (1, "答えを見る")
        taps = [see, (1, "言えなかった"), see, (1, "言えた"), see, (1, "迷った"), see]
        finished, its, _ = self.run_view(taps)
        self.assertEqual(finished, [True])
        self.assertEqual(its[0].labels, ["答えを見る"], "no rating before the answer")
        self.assertIn("答え: **Takk.**", its[0].edits[0][0])
        self.assertEqual(its[1].labels, ["言えた", "言えなかった", "迷った"])
        next_q = its[1].edits[0][0]
        self.assertIn("2/3", next_q)
        self.assertNotIn("Ég vil fara heim.", next_q, "the next answer is hidden again")
        self.assertIn("答え: **Ég vil fara heim.**", its[2].edits[0][0])
        self.assertIn("3/3問に回答", its[5].edits[0][0])
        self.assertIsNone(its[5].edits[0][1], "buttons removed at the end")
        self.assertEqual(its[6].edits, [], "a tap after the end is ignored")

    def test_only_the_owner_can_answer(self):
        finished, its, session = self.run_view([(2, "言えた")])
        self.assertEqual(its[0].messages, [("本人だけが回答できます。", True)])
        self.assertEqual((finished, session.results), ([], []))

    def test_the_review_cannot_be_skipped(self):
        """#38 review: /lesson generates only once the review is answered (the last lesson's
        new items above all); /lesson-auto is the way to generate without one."""
        _, its, _ = self.run_view([(1, "答えを見る")])
        self.assertTrue(all("振り返らずに生成" not in it.labels for it in its))
        self.assertEqual(its[0].labels, ["答えを見る"])

    def test_timeout_keeps_the_answers_and_the_rest(self):
        finished, _, session = self.run_view(
            [(1, "答えを見る"), (1, "迷った")], timeout=True
        )
        self.assertEqual(finished, ["expired"])
        self.assertEqual(session.saved, ["shaky", "unseen", "unseen"])

    def test_setup_registers_only_when_configured(self):
        client = discord.Client(intents=discord.Intents.default())
        self.assertIsNone(setup(client, SimpleNamespace()))
        config = SimpleNamespace(LESSON_ROOT="/data", LESSON_USERS={1: "yuki"})
        with mock.patch.object(discord.app_commands.CommandTree, "add_command") as add:
            self.assertTrue(callable(setup(client, config)))
        names = [call.args[0].name for call in add.call_args_list]
        self.assertEqual(
            names,
            [
                "lesson",
                "lesson-auto",
                "lesson-feedback",
                "lesson-feedback-report",
                "lesson-feedback-export",
                "lesson-week",
                "version",
            ],
        )


class ReportTests(unittest.TestCase):
    """PR #32 review: a session's answers are reported per source lesson."""

    def test_old_lessons_are_reported_as_themselves_not_as_the_latest(self):
        calls = []

        async def fake_cli(cfg, args, on_progress=None):
            calls.append(args)
            return 0, "", ""

        with tempfile.TemporaryDirectory() as td:
            cfg = LessonConfig(root=Path(td), users={1: "yuki"})
            path = cfg.pending_path("yuki")
            path.parent.mkdir(parents=True)
            queue = ReviewQueue(
                {
                    "a": Entry(["a"], "p", "A.", 3, due=D.isoformat()),
                    "b": Entry(["b"], "p", "B.", 7, due=D.isoformat()),
                    # lesson 10 is the latest; its question is not asked this time
                    "c": Entry(["c"], "p", "C.", 10, new=True, due="2026-10-30"),
                }
            )
            session = ReviewSession(queue, ["a", "b"], path, D)
            session.rate("failed")
            session.rate("ok")
            with mock.patch("src.lesson.run_cli", fake_cli):
                ok = asyncio.run(
                    Lessons(cfg).flush_reports(FakeChannel(), "yuki", queue, path)
                )
            self.assertTrue(ok)
            learner = str(cfg.learner_path("yuki"))
            self.assertEqual(
                calls,
                [
                    ["report", "--learner", learner, "--lesson", "3", "--failed", "a"],
                    [
                        "report",
                        "--learner",
                        learner,
                        "--lesson",
                        "7",
                        "--recalled",
                        "b",
                    ],
                ],
                "lesson 10 is not marked reported: none of its questions was answered",
            )
            self.assertEqual(ReviewQueue.load(path, D).reports(), [])


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
        self.views = []

    def typing(self):
        channel = self

        class Typing:
            async def __aenter__(self):
                return channel

            async def __aexit__(self, *exc):
                return False

        return Typing()

    async def send(self, content=None, files=None, view=None):
        self.sent.append((content, [f.filename for f in files or []]))
        self.views.append(view)
        for f in files or []:
            f.close()  # as discord.py does after sending


@unittest.skipUnless((LLA_DIR / "audiolesson").exists(), "submodule not checked out")
async def answer_all(view, first="言えなかった", second="言えた", rest="言えた"):
    """Answer every question of a review view: ``first`` for the first, ``second`` for the
    second, ``rest`` for the others. Returns the number of questions answered."""
    n = 0
    while not view.is_finished():
        await {b.label: b for b in view.children}["答えを見る"].callback(
            FakeInteraction(1)
        )
        label = first if n == 0 else second if n == 1 else rest
        await {b.label: b for b in view.children}[label].callback(FakeInteraction(1))
        n += 1
    return n


class EndToEndTests(unittest.TestCase):
    """実際の CLI (stub 音声) で 生成 → 投稿 → 振り返り → report → 次の生成."""

    def config(self, td, **kw):
        kw.setdefault("reading_cards", 0)  # 読みカードは test_reading_cards_* で
        kw.setdefault("scene_cards", 0)  # 場面カードと準備状況は test_scene_cards_* で
        kw.setdefault("readiness_days", 0)
        return LessonConfig(
            root=Path(td),
            users={1: "yuki"},
            minutes=3,
            extra_args=["--provider", "stub"],
            **kw,
        )

    @staticmethod
    def interaction(channel, replies):
        class Response:
            async def send_message(self, content, ephemeral=False, view=None):
                replies.append((content, view))

        async def original_response():
            return None

        async def edit_original_response(content=None, view=None):
            replies.append((content, view))

        return SimpleNamespace(
            user=SimpleNamespace(id=1),
            channel_id=5,
            channel=channel,
            response=Response(),
            original_response=original_response,
            edit_original_response=edit_original_response,
        )

    def test_lesson_week_lays_the_signals_side_by_side(self):
        """/lesson-week: the week's lessons, the new items' next-day answers, the feedback and
        the version used, for the learner only; scenario readiness follows as its own message.
        """
        with tempfile.TemporaryDirectory() as td:
            cfg = self.config(td, scene_cards=2)
            lessons = Lessons(
                cfg
            )  # the CLI dates lessons by the real day, so use it too
            channel = FakeChannel()
            asyncio.run(lessons.generate_and_post(channel, "yuki"))
            sent, deferred, refused = [], [], []

            class Response:
                async def defer(self, ephemeral=False, thinking=False):
                    deferred.append(ephemeral)

                async def send_message(self, content, ephemeral=False):
                    refused.append(content)

            class Followup:
                async def send(self, content, ephemeral=False):
                    sent.append((content, ephemeral))

            def interaction(user_id):
                return SimpleNamespace(
                    user=SimpleNamespace(id=user_id),
                    channel_id=5,
                    channel=channel,
                    response=Response(),
                    followup=Followup(),
                )

            asyncio.run(lessons.week(interaction(1), 7))
            self.assertEqual(deferred, [True])
            self.assertIn("直近 7 日のまとめ", sent[0][0])
            self.assertIn("レッスン 1〜1（1回）", sent[0][0])
            self.assertTrue(
                all(ephemeral for _, ephemeral in sent), "only the learner sees it"
            )
            self.assertIn(
                "旅行の準備", sent[-1][0], "readiness comes as a second message"
            )
            asyncio.run(lessons.week(interaction(99), 7))
            self.assertEqual(len(sent), 2, "an unregistered user gets no report")
            self.assertIn("登録されたユーザーだけ", refused[0])

    def test_review_report_and_next_lesson_through_discord(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.config(td, review_limit=2)
            day = {"today": D}
            lessons = Lessons(cfg, today=lambda: day["today"])
            channel = FakeChannel()
            asyncio.run(lessons.generate_and_post(channel, "yuki"))
            text, files = channel.sent[-1]
            self.assertIn("レッスン 1", text)
            # mp3 when ffmpeg is installed (the server, maybe CI), wav otherwise
            self.assertTrue({"lesson-001.wav", "lesson-001.mp3"} & set(files), files)
            self.assertIn("lesson-001.transcript.md", files)
            # every new item is asked next time, past the limit of 2
            self.assertIn("Discord 振り返り: 次回 3問（確認待ち 3件）", text)
            user = Path(td) / "yuki"
            self.assertTrue((user / "learner.json").exists())
            # the lesson guides to the feedback button; the GPT Voice check is gone (#129)
            self.assertIn("/lesson-feedback", text)
            self.assertNotIn("音声チェック", text)
            self.assertEqual(list((user / "work").iterdir()), [], "outputs removed")
            queued = ReviewQueue.load(cfg.pending_path("yuki"), D).entries
            self.assertGreater(len(queued), 2, "more questions than one session holds")

            day["today"] = D + timedelta(days=1)
            replies = []

            async def review():
                await lessons.start(self.interaction(channel, replies))
                view = replies[0][1]
                self.assertIsNotNone(view, "a review starts")
                new = [e for e in queued.values() if e.new]
                self.assertGreater(len(new), 2)
                self.assertIn(
                    f"振り返り 1/{len(new)}",
                    replies[0][0],
                    "every new item of the last lesson, past the limit of 2",
                )
                self.assertIn(
                    f"これから定着度チェックです（前回までの表現、全{len(new)}問）",
                    replies[0][0],
                )
                self.assertEqual(await answer_all(view, second="迷った"), len(new))

            asyncio.run(review())
            self.assertIn("レッスン 2", channel.sent[-1][0])
            self.assertEqual(lessons.busy, set())
            queue = ReviewQueue.load(cfg.pending_path("yuki"), day["today"])
            failed = [e for e in queue.entries.values() if e.state == "failed"]
            self.assertEqual(len(failed), 1)
            learner = json.loads(cfg.learner_path("yuki").read_text("utf-8"))
            for item_id in failed[0].items:
                self.assertEqual(learner["items"][item_id]["failures"], 1, "reported")
            shaky = [e for e in queue.entries.values() if e.state == "shaky"]
            self.assertEqual(len(shaky), 1)
            only_shaky = set(shaky[0].items) - set(failed[0].items)
            self.assertTrue(only_shaky)
            for item_id in only_shaky:
                # issue #119: hesitation reaches the audio lesson too
                self.assertEqual(learner["items"][item_id]["hesitated"], 1)
            unseen_before = {k for k, e in queued.items()} - {
                k for k, e in queue.entries.items() if e.state != "unseen"
            }
            self.assertTrue(unseen_before <= set(queue.entries), "nothing dropped")
            self.assertGreater(
                len(queue.entries), len(queued), "lesson 2 questions added"
            )

    def test_a_failed_report_is_sent_again_exactly_once(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.config(td, review_limit=2)
            day = {"today": D}
            lessons = Lessons(cfg, today=lambda: day["today"])
            channel = FakeChannel()
            asyncio.run(lessons.generate_and_post(channel, "yuki"))
            day["today"] = D + timedelta(days=1)
            reports = {"left_to_fail": 1}

            async def flaky_cli(cfg_, args, on_progress=None):
                if args[0] == "report" and reports["left_to_fail"]:
                    reports["left_to_fail"] -= 1
                    return 1, "", "disk full"
                return await run_cli(cfg_, args, on_progress)

            async def lesson_command(first="言えた"):
                replies = []
                await lessons.start(self.interaction(channel, replies))
                view = replies[0][1]
                if view is not None:
                    await answer_all(view, first=first)

            failed_items: list[str] = []

            def failures():
                learner = json.loads(cfg.learner_path("yuki").read_text("utf-8"))
                queue = ReviewQueue.load(cfg.pending_path("yuki"), day["today"])
                if not failed_items:  # the question answered 言えなかった, found once
                    failed = next(
                        e for e in queue.entries.values() if e.state == "failed"
                    )
                    failed_items.extend(failed.items)
                return [learner["items"][i]["failures"] for i in failed_items], queue

            with mock.patch("src.lesson.run_cli", flaky_cli):
                asyncio.run(lesson_command(first="言えなかった"))
            self.assertIn("report できませんでした", channel.sent[-1][0])
            counts, queue = failures()
            self.assertEqual(set(counts), {0}, "not in learner.json yet")
            self.assertEqual([n for n, _ in queue.reports()], [1], "kept for a retry")
            self.assertNotIn("レッスン 2", " ".join(t or "" for t, _ in channel.sent))

            with mock.patch("src.lesson.run_cli", flaky_cli):
                asyncio.run(lesson_command())  # the next /lesson
            counts, queue = failures()
            self.assertEqual(set(counts), {1}, "delivered on the next /lesson")
            self.assertEqual(queue.reports(), [])
            self.assertIn("レッスン 2", channel.sent[-1][0])

            with mock.patch("src.lesson.run_cli", flaky_cli):
                asyncio.run(lesson_command())
            self.assertEqual(set(failures()[0]), {1}, "and never sent twice")

    def test_reading_cards_and_the_trip_profile_through_discord(self):
        """#133: the review ends with reading cards in place of some questions (the same
        total); #132: a trip.toml is passed to generate, and the lesson record keeps only
        its sha256. Nothing from the profile is written by the bot."""
        with tempfile.TemporaryDirectory() as td:
            cfg = self.config(td, review_limit=20, reading_cards=3)
            user = cfg.user_dir("yuki")
            user.mkdir(parents=True)
            trip = cfg.trip_path("yuki")
            trip.write_text(
                'places = ["Testvík"]\nseason = "winter-holidays"\n', "utf-8"
            )
            day = {"today": D}
            lessons = Lessons(cfg, today=lambda: day["today"])
            channel = FakeChannel()
            asyncio.run(lessons.generate_and_post(channel, "yuki"))
            self.assertIn("レッスン 1", channel.sent[-1][0])
            record = feedback.Ledger(user).load(None)
            self.assertIn("--trip", record.manifest["generate_args"])
            self.assertEqual(
                record.manifest["trip_sha256"],
                hashlib.sha256(trip.read_bytes()).hexdigest(),
            )
            self.assertEqual(record.plan["config"]["priority_items"] > 0, True)

            day["today"] = D + timedelta(days=1)
            queued = ReviewQueue.load(cfg.pending_path("yuki"), day["today"])
            new = [e for e in queued.entries.values() if e.new]
            replies = []

            async def review():
                await lessons.start(self.interaction(channel, replies))
                self.assertEqual(replies[0], ("振り返りを準備しています…", None))
                text, view = replies[-1]
                self.assertIn(f"全{len(new)}問", text)
                self.assertIn("読みカードが3枚", text)
                return await answer_all(view, first="言えた", second="言えなかった")

            answered = asyncio.run(review())
            self.assertEqual(answered, min(len(new) + 3, 20))
            self.assertIn("レッスン 2", channel.sent[-1][0])
            saved = json.loads(cfg.reading_path("yuki").read_text("utf-8"))
            self.assertEqual(len(saved["cards"]), answered - len(new))
            learner = json.loads(cfg.learner_path("yuki").read_text("utf-8"))
            self.assertFalse(
                set(saved["cards"]) & set(learner["items"]),
                "reading cards are not reported to learner.json",
            )
            # nothing the bot wrote holds the private place name
            for p in Path(td).rglob("*"):
                if p.is_file() and p != trip:
                    self.assertNotIn(b"Testv", p.read_bytes(), p)

    def test_scene_cards_and_the_readiness_summary_through_discord(self):
        """#129: scenario cards follow the questions, before the reading cards, within the
        same review length; the lesson post carries the weekly readiness summary."""
        with tempfile.TemporaryDirectory() as td:
            cfg = self.config(
                td, review_limit=20, scene_cards=2, reading_cards=1, readiness_days=7
            )
            day = {"today": D}
            lessons = Lessons(cfg, today=lambda: day["today"])
            channel = FakeChannel()
            asyncio.run(lessons.generate_and_post(channel, "yuki"))
            text = channel.sent[-1][0]
            self.assertIn("レッスン 1", text)
            self.assertIn("旅行の準備（場面カード）", text)
            self.assertIn("Tier A（10場面）", text)

            day["today"] = D + timedelta(days=1)
            queued = ReviewQueue.load(cfg.pending_path("yuki"), day["today"])
            new = [e for e in queued.entries.values() if e.new]
            replies = []

            async def review():
                await lessons.start(self.interaction(channel, replies))
                text, view = replies[-1]
                self.assertIn(f"全{len(new)}問", text)
                self.assertIn("場面カードが", text)
                return await answer_all(view)

            answered = asyncio.run(review())
            scenes_done = json.loads(cfg.scene_path("yuki").read_text("utf-8"))["cards"]
            self.assertTrue(scenes_done, "lesson 1 taught enough for a scene card")
            self.assertLessEqual(len(scenes_done), 2)
            reading_done = json.loads(cfg.reading_path("yuki").read_text("utf-8"))[
                "cards"
            ]
            self.assertEqual(answered, len(new) + len(scenes_done) + len(reading_done))
            self.assertLessEqual(answered, 20)
            self.assertIn("レッスン 2", channel.sent[-1][0])
            self.assertNotIn("旅行の準備", channel.sent[-1][0], "once a week")
            learner = json.loads(cfg.learner_path("yuki").read_text("utf-8"))
            self.assertFalse(set(scenes_done) & set(learner["items"]), "not reported")

    def test_trip_from_the_channel_topic(self):
        """No trip.toml: the [trip] in the channel topic is used. The record keeps
        «channel-topic» in place of the temporary path, and its sha256; nothing the bot
        writes holds the topic's contents. A broken topic: a notice, then no trip."""
        with tempfile.TemporaryDirectory() as td:
            cfg = self.config(td, review_limit=20, reading_cards=0)
            lessons = Lessons(cfg, today=lambda: D)
            channel = FakeChannel()
            channel.topic = (
                "旅行チャンネル\n[trip]\n"
                'places = ["Testvík"]\nseason = "winter-holidays"\n'
            )
            asyncio.run(lessons.generate_and_post(channel, "yuki"))
            self.assertIn("レッスン 1", channel.sent[-1][0])
            user = cfg.user_dir("yuki")
            record = feedback.Ledger(user).load(None)
            args = record.manifest["generate_args"]
            self.assertEqual(args[args.index("--trip") + 1], "channel-topic")
            canonical = 'places = ["Testvík"]\nseason = "winter-holidays"\n'
            self.assertEqual(
                record.manifest["trip_sha256"],
                hashlib.sha256(canonical.encode()).hexdigest(),
            )
            self.assertGreater(record.plan["config"]["priority_items"], 0)
            deck = asyncio.run(lessons.reading_deck("yuki", channel))
            self.assertEqual([c["text"] for c in deck if c.get("own")], ["Testvík"])
            for p in Path(td).rglob("*"):
                if p.is_file():
                    self.assertNotIn(b"Testv", p.read_bytes(), p)
                    self.assertNotIn(b"winter-holidays", p.read_bytes(), p)

            channel.topic = '[trip]\nhotel = "Testvík"\n'
            channel.sent.clear()
            asyncio.run(lessons.generate_and_post(channel, "yuki"))
            self.assertIn("トピックの旅程の設定を読めませんでした", channel.sent[0][0])
            self.assertNotIn("Testv", channel.sent[0][0])
            self.assertIn("レッスン 2", channel.sent[-1][0])
            record = feedback.Ledger(user).load(None)
            self.assertNotIn("--trip", record.manifest["generate_args"])
            self.assertIsNone(record.manifest["trip_sha256"])

    def test_levers_from_the_channel_topic(self):
        """A [levers] section in the topic overrides LESSON_EXTRA_ARGS' levers for generate;
        the lesson record and plan.json show what was used."""
        with tempfile.TemporaryDirectory() as td:
            cfg = self.config(td)
            cfg.extra_args += ["--pause-multiplier", "1.5"]
            lessons = Lessons(cfg, today=lambda: D)
            channel = FakeChannel()
            channel.topic = (
                "旅行チャンネル\n[levers]\npause_multiplier = 1.2\n"
                "late_unhinted_recall = true\n"
            )
            asyncio.run(lessons.generate_and_post(channel, "yuki"))
            self.assertIn("レッスン 1", channel.sent[-1][0])
            record = feedback.Ledger(cfg.user_dir("yuki")).load(None)
            args = record.manifest["generate_args"]
            self.assertEqual(args.count("--pause-multiplier"), 1)
            self.assertEqual(args[args.index("--pause-multiplier") + 1], "1.2")
            self.assertIn("--late-unhinted-recall", args)
            self.assertTrue(record.plan["config"]["levers"]["late_unhinted_recall"])

    def test_reading_deck_includes_own_places_only_when_allowed(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.config(td, reading_cards=5)
            cfg.user_dir("yuki").mkdir(parents=True)
            cfg.trip_path("yuki").write_text('places = ["Testvík"]\n', "utf-8")
            deck = asyncio.run(Lessons(cfg).reading_deck("yuki"))
            own = [c for c in deck if c.get("own")]
            self.assertEqual([c["text"] for c in own], ["Testvík"])
            self.assertTrue(all(c["id"].startswith("own_") for c in own))
            self.assertGreater(len(deck), 80)
            cfg.reading_own_places = False
            deck = asyncio.run(Lessons(cfg).reading_deck("yuki"))
            self.assertFalse([c for c in deck if c.get("own")])

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
            legacy_file(cfg.pending_path("yuki"), lesson=9)
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
            queue = ReviewQueue.load(cfg.pending_path("yuki"), date.today())
            self.assertTrue({"takk", "bless"} <= set(queue.entries), "queue kept")
            self.assertTrue(
                any(e.source_lesson == 1 for e in queue.entries.values()),
                "the new lesson's questions are queued for a later /lesson",
            )
            self.assertTrue(
                cfg.pending_path("yuki")
                .with_name("pending_review.json.v1.bak")
                .exists()
            )
            self.assertEqual(lessons.busy, set())


if __name__ == "__main__":
    unittest.main()


class OpenItemQueueTests(unittest.TestCase):
    """language-learning-audio #149: an item the audio keeps open (``plan.open_items``) is asked
    again the next day, whatever interval its question had reached."""

    def queue_with_ok_takk(self):
        q = ReviewQueue()
        q.add_from_plan(
            {
                "lesson_number": 1,
                "new_items": [{"id": "takk"}],
                "review": QUESTIONS[:1],
            },
            D,
        )
        for _ in range(3):
            q.record("takk", "ok", D)  # interval now 7 days
        return q

    def test_an_open_item_is_asked_again_tomorrow_despite_an_ok_streak(self):
        q = self.queue_with_ok_takk()
        later = D + timedelta(days=2)
        self.assertEqual(q.select(later), [])
        q.add_from_plan(
            {
                "lesson_number": 2,
                "new_items": [],
                "open_items": ["takk"],
                "review": QUESTIONS[:1],
            },
            later,
        )
        self.assertEqual(q.entries["takk"].due, (later + timedelta(days=1)).isoformat())
        self.assertEqual(q.entries["takk"].state, "ok", "its history stays")
        self.assertEqual(q.select(later + timedelta(days=1)), ["takk"])

    def test_a_full_review_still_asks_the_open_item(self):
        """Pulled forward, an ``ok`` question must not stay at the lowest priority: with the
        review limit full of other due questions it is still selected, and answering clears it.
        """
        q = self.queue_with_ok_takk()
        later = D + timedelta(days=2)
        fillers = [
            {"items": [f"f{n}"], "prompt": f"p{n}", "answer": f"a{n}"} for n in range(6)
        ]
        q.add_from_plan(
            {"lesson_number": 2, "new_items": [], "review": fillers},
            later - timedelta(days=1),
        )
        for n in range(6):
            q.entries[f"f{n}"].state = "shaky" if n % 2 else "ok"
        q.add_from_plan(
            {
                "lesson_number": 3,
                "new_items": [],
                "open_items": ["takk"],
                "review": QUESTIONS[:1],
            },
            later,
        )
        day = later + timedelta(days=1)
        self.assertIn("takk", q.select(day, 3))
        q.record("takk", "ok", day)
        self.assertFalse(q.entries["takk"].open)

    def test_without_open_items_a_queued_question_keeps_its_date(self):
        q = self.queue_with_ok_takk()
        due = q.entries["takk"].due
        q.add_from_plan(
            {"lesson_number": 2, "new_items": [], "review": QUESTIONS[:1]},
            D + timedelta(days=2),
        )
        self.assertEqual(q.entries["takk"].due, due)

    def test_an_open_item_without_a_question_gets_one_even_if_queued_elsewhere(self):
        q = ReviewQueue()
        q.add_from_plan(
            {"lesson_number": 1, "new_items": [], "review": [QUESTIONS[1]]}, D
        )
        extra = {"items": ["fara_heim"], "prompt": "家へ", "answer": "Heim."}
        q.add_from_plan(
            {
                "lesson_number": 2,
                "new_items": [],
                "open_items": ["fara_heim"],
                "review": [extra],
            },
            D,
        )
        self.assertIn("fara_heim", q.entries)
        self.assertEqual(
            q.entries["fara_heim"].due, (D + timedelta(days=1)).isoformat()
        )


class GuidanceTests(unittest.TestCase):
    def test_review_intro_only_on_the_first_question(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pending_review.json"
            queue = ReviewQueue.load(path, D)
            queue.add_from_plan(
                {"lesson_number": 1, "new_items": [], "review": QUESTIONS}, D
            )
            keys = queue.select(D, 10)
            session = ReviewSession(queue, keys, path, D)
            self.assertTrue(session.render().startswith("これから定着度チェックです"))
            self.assertIn("これから定着度チェック", session.render(revealed=True))
            session.rate("ok")
            self.assertNotIn("これから定着度チェック", session.render())
