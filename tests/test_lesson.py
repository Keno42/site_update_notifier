"""src/lesson.py のテスト. 実行: python -m unittest discover -s tests -t ."""

import asyncio
import json
import subprocess
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
from src.interaction import STALE_RATING
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

    def test_a_failed_screen_update_after_a_rating_keeps_the_review_going(self):
        """#80 review: the rating is recorded before the screen is updated. If the update fails (an expired token) the next
        question is sent again as a new message with working buttons, and on the last one the report and the lesson still run.
        """

        async def scenario(td):
            finished, sent = [], []

            async def finish(generate):
                finished.append(generate)

            async def expire():
                finished.append("expired")

            session = session_on(td)
            view = ReviewView(session, 1, finish, expire)

            def broken(it):
                async def edit_message(**kw):
                    raise RuntimeError("Unknown interaction")

                it.response.edit_message = edit_message

                class Followup:
                    async def send(self, content=None, view=None, wait=False, **kw):
                        sent.append((content, view))
                        return SimpleNamespace(edit=None)

                it.followup = Followup()
                return it

            async def tap(label):
                it = broken(FakeInteraction(1))
                buttons = {b.label: b for b in view.children}
                await buttons[label].callback(it)

            with self.assertLogs(level="ERROR"):
                for label in [
                    "答えを見る",
                    "言えた",
                    "答えを見る",
                    "言えた",
                    "答えを見る",
                    "言えた",
                ]:
                    await tap(label)
            return finished, sent, view, session

        with tempfile.TemporaryDirectory() as td:
            finished, sent, view, session = asyncio.run(scenario(td))
        self.assertEqual(
            finished, [True], "the last answer still reports and generates"
        )
        self.assertTrue(
            any(v is view and "2/3" in c for c, v in sent),
            "the next question comes again, with buttons",
        )
        self.assertEqual(len(session.results), 3, "every rating was recorded")
        self.assertIsNotNone(view.message)

    def test_an_old_messages_rating_button_does_not_rate_the_next_question(self):
        """#80 review: after a failed screen update the question is sent again as a
        new message, and the old message's buttons still dispatch. A tap on the old
        rating button must not record the question the learner has not seen."""

        async def scenario(td):
            async def noop(generate=None):
                return None

            session = session_on(td)
            view = ReviewView(session, 1, noop, noop)

            def broken(it):
                async def edit_message(**kw):
                    raise RuntimeError("Unknown interaction")

                it.response.edit_message = edit_message

                class Followup:
                    async def send(self, content=None, view=None, wait=False, **kw):
                        return SimpleNamespace(edit=None)

                it.followup = Followup()
                return it

            reveal = next(b for b in view.children if b.label == "答えを見る")
            await reveal.callback(FakeInteraction(1))
            # the rating buttons of question 1
            old = {b.label: b for b in view.children}
            with self.assertLogs(level="ERROR"):
                await old["言えた"].callback(broken(FakeInteraction(1)))
            self.assertEqual(len(session.results), 1)
            again = FakeInteraction(1)
            await old["言えなかった"].callback(again)  # the old message, now stale
            return session, again

        with tempfile.TemporaryDirectory() as td:
            session, again = asyncio.run(scenario(td))
        self.assertEqual(len(session.results), 1, "the stale tap rated nothing")
        self.assertEqual(again.messages, [(STALE_RATING, True)])

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
                "lesson-configure",
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


class FakeMessage:
    """What ``channel.send`` returns: an id (the lesson post is replied to) and a no-op edit."""

    def __init__(self, id):
        self.id = id

    async def edit(self, **kw):
        pass

    async def delete(self):
        pass


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

    id = 5

    async def send(self, content=None, files=None, view=None, **kw):
        self.sent.append((content, [f.filename for f in files or []]))
        self.views.append(view)
        self.references = getattr(self, "references", []) + [kw.get("reference")]
        for f in files or []:
            f.close()  # as discord.py does after sending
        return FakeMessage(1000 + len(self.sent))


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


class RefineQueueTests(unittest.TestCase):
    """site_update_notifier#96: the one-time pass over the queue with `audiolesson refine-review`. The CLI is replaced by a canned answer: the rules themselves
    are language-learning-audio's (its own tests) and `RefinementTests` in test_review_queue."""

    def config(self, td):
        return LessonConfig(root=Path(td), users={1: "yuki"}, default_minutes=5, default_new_list="after", default_order="spread")

    def queue(self):
        q = ReviewQueue()
        q.entries = {
            "matinn": Entry(["matinn"], "for the meal", "matinn", 20, state="failed", due=D.isoformat(), open=True),
            "eigdu_godan_dag": Entry(["eigdu_godan_dag"], "p", "Eigðu góðan dag.", 20, state="ok", due=D.isoformat()),
            "eigdu_godur": Entry(["eigdu_godur"], "q", "Eigðu góðan dag.", 20, state="ok", due=(D + timedelta(days=5)).isoformat()),
        }
        return q

    refined = {
        "review": [
            {"items": ["matinn"], "prompt": "Say in Icelandic: Thanks for the meal.", "answer": "Takk fyrir matinn."},
            {"items": ["eigdu_godan_dag", "eigdu_godur"], "prompt": "p", "answer": "Eigðu góðan dag."},
        ],
        "refined": [{"items": ["matinn"], "kind": "through_whole"}, {"items": ["eigdu_godur"], "kind": "same_answer"}],
    }

    def run_pass(self, rc, out):
        calls = []

        async def fake_run_cli(cfg, args, on_progress=None, stdin=None):
            calls.append((args, stdin))
            return rc, out, ""

        with tempfile.TemporaryDirectory() as td:
            lessons = Lessons(self.config(td), today=lambda: D)
            path = Path(td) / "pending_review.json"
            queue = self.queue()
            with mock.patch("src.lesson.run_cli", fake_run_cli):
                asyncio.run(lessons.refine_queue("yuki", queue, D, path))
                asyncio.run(lessons.refine_queue("yuki", queue, D, path))  # the second time asks nothing
            return queue, calls, path.exists()

    def test_the_queue_is_refined_once_and_the_version_is_saved(self):
        queue, calls, saved = self.run_pass(0, json.dumps(self.refined))
        self.assertEqual(len(calls), 1)
        args, stdin = calls[0]
        self.assertEqual(args[0], "refine-review")
        self.assertIn("-l", args)
        sent = json.loads(stdin)["review"]
        self.assertEqual([q["items"] for q in sent], [["matinn"], ["eigdu_godan_dag"], ["eigdu_godur"]])
        self.assertEqual(queue.entries["matinn"].answer, "Takk fyrir matinn.")
        self.assertTrue(queue.entries["matinn"].open)
        self.assertEqual(list(queue.entries), ["matinn", "eigdu_godan_dag+eigdu_godur"])
        self.assertEqual((queue.refined, saved), (1, True))

    def test_an_audio_side_that_cannot_refine_leaves_the_queue_as_it_was_and_tries_again(self):
        queue, calls, saved = self.run_pass(2, "")
        self.assertEqual((len(calls), queue.refined, saved), (2, 0, False), "asked both times: nothing was marked done")
        self.assertEqual(queue.entries["matinn"].answer, "matinn")
        queue, calls, saved = self.run_pass(0, "not json")
        self.assertEqual((queue.refined, saved), (0, False))


class EndToEndTests(unittest.TestCase):
    """実際の CLI (stub 音声) で 生成 → 投稿 → 振り返り → report → 次の生成."""

    def config(self, td, **kw):
        kw.setdefault("reading_cards", 0)  # 読みカードは test_reading_cards_* で
        kw.setdefault("scene_cards", 0)  # 場面カードと準備状況は test_scene_cards_* で
        kw.setdefault("readiness_days", 0)
        kw.setdefault("minutes", 3)  # 振り返りの量はレッスンの長さに比例する (usersettings)
        return LessonConfig(
            root=Path(td),
            users={1: "yuki"},
            default_minutes=kw.get("minutes", 3),
            default_new_list="after",
            default_order="spread",
            extra_args=["--provider", "stub", "--new", "3"],
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
            # 5 minutes: a part is now introduced inside its whole (language-learning-audio #239), a longer exercise, so three new items no longer fit in 3
            cfg = self.config(td, review_limit=2, minutes=5)
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
            self.assertEqual(lessons.busy, {})
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
            cfg = self.config(
                td,
                review_limit=20,
                reading_cards=3,
                minutes=30,
                upload_limit_mb=1000,
            )
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
                td,
                review_limit=20,
                scene_cards=2,
                reading_cards=1,
                readiness_days=7,
                minutes=30,
                upload_limit_mb=1000,
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
            # the previous lesson's new-expression list (#79) comes first; the warning is among the messages
            self.assertTrue(any("トピックの旅程の設定を読めませんでした" in t for t, _ in channel.sent))
            for text, _ in channel.sent:
                self.assertNotIn("Testv", text)
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
                default_minutes=3, default_new_list="after", default_order="spread",
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

    def _review_started(self, td, lessons_cls=Lessons):
        """Lesson 1 posted, the next day's /lesson started: the review is on screen, busy is held."""
        cfg = self.config(td, review_limit=2)
        day = {"today": D}
        lessons = lessons_cls(cfg, today=lambda: day["today"])
        channel = FakeChannel()
        asyncio.run(lessons.generate_and_post(channel, "yuki"))
        day["today"] = D + timedelta(days=1)
        replies = []
        asyncio.run(lessons.start(self.interaction(channel, replies)))
        self.assertEqual(set(lessons.busy), {"yuki"})
        return lessons, channel, replies[0][1]

    def test_finish_releases_busy_even_when_the_timing_record_raises(self):
        """#82: the log is an aid. A failure in it must not stop the report and the next lesson, nor keep «in progress»."""
        from src.review import ReviewSession

        with tempfile.TemporaryDirectory() as td:
            lessons, channel, view = self._review_started(td)
            before = len(channel.sent)
            with mock.patch.object(ReviewSession, "timing_record", side_effect=ValueError("boom")):
                asyncio.run(view.finish(True))
            self.assertEqual(lessons.busy, {})
            self.assertGreater(len(channel.sent), before, "the next lesson is still generated")

    def test_a_stale_busy_entry_does_not_block_lesson_but_a_fresh_one_does(self):
        import time as _time

        with tempfile.TemporaryDirectory() as td:
            cfg = self.config(td, review_limit=2)
            lessons = Lessons(cfg)
            channel = FakeChannel()
            replies = []
            refused = []

            class Response:
                async def send_message(self, content, ephemeral=False, view=None):
                    refused.append(content)

            def interaction():
                it = self.interaction(channel, replies)
                it.response = Response()
                return it

            lessons.busy["yuki"] = _time.monotonic()
            asyncio.run(lessons.start(interaction()))
            self.assertEqual(refused, ["前の /lesson がまだ進行中です。"])
            self.assertIn("yuki", lessons.busy)

            lessons.busy["yuki"] = _time.monotonic() - lessons.busy_limit() - 1
            with self.assertLogs(level="WARNING") as logs:
                asyncio.run(lessons.start(interaction()))
            self.assertEqual(len(refused), 2, "the stale entry let /lesson go on")
            self.assertNotEqual(refused[1], refused[0])
            self.assertTrue(any("進行中の記録を捨てます" in m for m in logs.output))

    def test_auto_skips_review_and_self_report(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = LessonConfig(
                root=Path(td),
                users={1: "yuki"},
                minutes=3,
                default_minutes=3, default_new_list="after", default_order="spread",
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
            self.assertEqual(lessons.busy, {})


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

    def test_every_open_item_of_a_plan_is_brought_forward(self):
        """language-learning-audio #199: the plan carries ``open_items`` (it did not before), and every one of them
        is asked tomorrow, each with its own question, however far its entry had been pushed.
        """
        q = ReviewQueue()
        ids = [f"o{n}" for n in range(5)]
        qs = [{"items": [i], "prompt": f"p{i}", "answer": f"a{i}"} for i in ids]
        q.add_from_plan({"lesson_number": 1, "new_items": [], "review": qs}, D)
        for e in q.entries.values():
            e.state, e.due = "ok", (D + timedelta(days=30)).isoformat()
        today = D + timedelta(days=2)
        q.add_from_plan(
            {
                "lesson_number": 2,
                "new_items": [],
                "open_items": ids,
                "open_not_fitted": [],
                "review": qs,
            },
            today,
        )
        tomorrow = (today + timedelta(days=1)).isoformat()
        self.assertEqual(
            [i for i in ids if q.entries[i].due == tomorrow and q.entries[i].open], ids
        )
        self.assertEqual(sorted(q.select(today + timedelta(days=1), 10)), sorted(ids))

    def test_every_waiting_open_item_is_asked(self):
        """#220 (owner, 2026-10-08): items that didn't fit the lesson (``open_not_fitted``) are all asked in
        the review, in no smaller number than the practised ones. An item with no queued question gets the plan's
        own (the audio side writes one for each); one the plan brings no question for can't be asked."""
        q = ReviewQueue()
        ids = [f"w{n}" for n in range(6)]
        qs = [{"items": [i], "prompt": f"p{i}", "answer": f"a{i}"} for i in ids]
        q.add_from_plan({"lesson_number": 1, "new_items": [], "review": qs}, D)
        for e in q.entries.values():
            e.state, e.due = "ok", (D + timedelta(days=14)).isoformat()
        day = D + timedelta(days=1)
        waiting = ["nothing_queued", "w0", "w1", "w2", "w3", "w4", "no_question_here"]
        plan = {
            "lesson_number": 2,
            "new_items": [],
            "open_items": [],
            "open_not_fitted": waiting,
            "review": [
                {"items": ["nothing_queued"], "prompt": "pn", "answer": "an", "stage": "open"}
            ],
        }
        q.add_from_plan(plan, day)
        tomorrow = (day + timedelta(days=1)).isoformat()
        self.assertEqual(
            [k for k, e in q.entries.items() if e.open],
            ["w0", "w1", "w2", "w3", "w4", "nothing_queued"],
        )
        self.assertTrue(all(q.entries[k].due == tomorrow for k in ("w0", "w4", "nothing_queued")))
        self.assertEqual(q.entries["w5"].due, (D + timedelta(days=14)).isoformat())
        self.assertFalse(q.entries["w5"].open)
        # all six are required at the next review, whatever the limit
        later = day + timedelta(days=1)
        self.assertEqual(
            sorted(q.select(later, limit=2)),
            sorted(["w0", "w1", "w2", "w3", "w4", "nothing_queued"]),
        )

    def test_an_item_that_closed_is_no_longer_asked(self):
        q = ReviewQueue()
        q.add_from_plan(
            {"lesson_number": 1, "new_items": [], "open_items": ["a", "b"],
             "review": [{"items": [i], "prompt": f"p{i}", "answer": f"a{i}"} for i in "ab"]},
            D,
        )
        self.assertEqual([k for k, e in q.entries.items() if e.open], ["a", "b"])
        q.add_from_plan(
            {"lesson_number": 2, "new_items": [], "open_items": ["b"], "open_not_fitted": [], "review": []},
            D,
        )
        self.assertEqual([k for k, e in q.entries.items() if e.open], ["b"])

    def test_only_one_question_per_open_item_is_required(self):
        """The one with the fewest items (then the newest lesson) is chosen; the other questions
        that contain the item stay ordinary."""
        q = ReviewQueue()
        q.add_from_plan(
            {"lesson_number": 1, "new_items": [], "review": [QUESTIONS[1]]},
            D,
        )
        q.add_from_plan(
            {"lesson_number": 2, "new_items": [], "open_items": ["fara_heim"],
             "review": [{"items": ["fara_heim"], "prompt": "p", "answer": "a", "stage": "open"}]},
            D,
        )
        self.assertEqual([k for k, e in q.entries.items() if e.open], ["fara_heim"])
        self.assertEqual(q.must_answer(D + timedelta(days=1)), ["fara_heim"])
        q.record("fara_heim", "failed", D + timedelta(days=1))
        self.assertEqual(q.must_answer(D + timedelta(days=1)), [], "a retry the same day isn't asked twice")

    def test_with_ten_old_questions_waiting_every_open_item_is_still_asked_once(self):
        """#220 T2: tier-1 entries overdue by weeks used to take every slot."""
        q = ReviewQueue()
        old = {f"old{n}": Entry([f"old{n}"], "p", "a", 1, state="failed", due=(D - timedelta(days=30 + n)).isoformat()) for n in range(10)}
        q.entries.update(old)
        opens = ["x", "y"]
        q.add_from_plan(
            {"lesson_number": 5, "new_items": [], "open_items": ["x"], "open_not_fitted": ["y"],
             "review": [{"items": [i], "prompt": f"p{i}", "answer": f"a{i}"} for i in opens]},
            D,
        )
        picked = q.select(D + timedelta(days=1), limit=3)
        self.assertEqual(sorted(picked[:2]), ["x", "y"])
        self.assertEqual(len(picked), len(set(picked)))
        self.assertEqual(len(picked), 3, "the limit still holds for the rest")

    def test_a_re_asked_question_gets_the_plans_wording_and_keeps_its_schedule(self):
        """#73 point 1: the stored wording follows the plan; due, state, streak, reviews and
        source_lesson stay, so the report still goes to the lesson that asked it."""
        q = ReviewQueue()
        q.add_from_plan({"lesson_number": 3, "new_items": [], "review": [{"items": ["sofa"], "prompt": "How do you say: Sleep.", "answer": "sofa"}]}, D)
        q.record("sofa", "ok", D)
        before = (q.entries["sofa"].due, q.entries["sofa"].state, q.entries["sofa"].streak, q.entries["sofa"].reviews)
        q.add_from_plan({"lesson_number": 9, "new_items": [], "review": [{"items": ["sofa"], "prompt": "Say: To sleep.", "answer": "sofa"}]}, D + timedelta(days=2))
        e = q.entries["sofa"]
        self.assertEqual(e.prompt, "Say: To sleep.")
        self.assertEqual((e.due, e.state, e.streak, e.reviews), before)
        self.assertEqual(e.source_lesson, 3)

    def test_a_question_with_an_unfilled_template_is_not_shown(self):
        """#73 point 2 / B5: «{hour}» left in a stored prompt is skipped (and not counted as waiting)."""
        q = ReviewQueue()
        q.entries["klukkan_er"] = Entry(["klukkan_er"], "Klukkan er {hour}.", "Klukkan er tvö.", 1, due=D.isoformat())
        q.entries["ok"] = Entry(["ok"], "p", "a", 1, due=D.isoformat())
        self.assertEqual(q.select(D, limit=5), ["ok"])
        self.assertEqual(q.due_count(D), 1)

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


class OpenItemWordingTests(unittest.TestCase):
    """#73 / #220 B4: a one-item question is worded again from the course just before it is shown."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.cfg = LessonConfig(root=Path(self.td.name), users={1: "yuki"})
        self.path = self.cfg.pending_path("yuki")
        self.path.parent.mkdir(parents=True)
        self.queue = ReviewQueue(
            {
                "sofa": Entry(["sofa"], "How do you say: Sleep.", "sofa", 4, due=D.isoformat(), reviews=2, state="ok"),
                "eg_vil+fara_heim": Entry(["eg_vil", "fara_heim"], "帰りたい", "Ég vil fara heim.", 4, due=D.isoformat()),
                "later": Entry(["later"], "p", "a", 4, due=(D + timedelta(days=9)).isoformat()),
            }
        )

    def refresh(self, fake_cli):
        with mock.patch("src.lesson.run_cli", fake_cli):
            asyncio.run(Lessons(self.cfg).refresh_wording(self.queue, D, self.path))

    def test_the_stored_wording_is_replaced_by_the_courses_current_cue(self):
        calls = []

        async def fake_cli(cfg, args, on_progress=None):
            calls.append(args)
            return 0, json.dumps({"sofa": {"prompt": "「寝る」と言ってください。", "answer": "sofa", "cues": ["「寝る」と言ってください。"]}}), ""

        self.refresh(fake_cli)
        e = self.queue.entries["sofa"]
        self.assertEqual((e.prompt, e.answer), ("「寝る」と言ってください。", "sofa"))
        self.assertEqual((e.due, e.state, e.reviews, e.source_lesson), (D.isoformat(), "ok", 2, 4))
        self.assertEqual(self.queue.entries["eg_vil+fara_heim"].prompt, "帰りたい", "a two-item question is left alone")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][calls[0].index("--ids") + 1], "sofa", "only the due single-item questions are asked for")
        self.assertEqual(ReviewQueue.load(self.path, D).entries["sofa"].prompt, "「寝る」と言ってください。", "saved")

    def test_a_sentence_question_keyed_by_one_item_keeps_its_answer(self):
        """The review of #88: «tvo» asked as a sentence stays a sentence; a bare entry whose prompt is another current cue stays too."""
        self.queue.entries["tvo"] = Entry(["tvo"], "「2000クローナです」と言ってください。", "Það kostar tvö þúsund krónur.", 4, due=D.isoformat())
        self.queue.entries["gott"] = Entry(["gott"], "「良い」と言ってください。", "gott", 4, due=D.isoformat())

        async def fake_cli(cfg, args, on_progress=None):
            return 0, json.dumps({
                "tvo": {"prompt": "「2」と言ってください。", "answer": "tvö", "cues": ["「2」と言ってください。"]},
                "gott": {"prompt": "「良い」(状況0)", "answer": "gott", "cues": ["「良い」(状況0)", "「良い」と言ってください。"]},
                "sofa": {"prompt": "「寝る」", "answer": "sofa", "cues": ["「寝る」"]},
            }), ""

        self.refresh(fake_cli)
        self.assertEqual(self.queue.entries["tvo"].answer, "Það kostar tvö þúsund krónur.")
        self.assertEqual(self.queue.entries["tvo"].prompt, "「2000クローナです」と言ってください。")
        self.assertEqual(self.queue.entries["gott"].prompt, "「良い」と言ってください。", "another current cue: variety kept")
        self.assertEqual(self.queue.entries["sofa"].prompt, "「寝る」", "a stale bare question is reworded")

    def test_without_cues_only_an_unaskable_question_is_reworded(self):
        """An older audiolesson prints no «cues»: nothing current is overwritten, only what cannot be asked."""
        self.queue.entries["gott"] = Entry(["gott"], "「良い」と言ってください。", "gott", 4, due=D.isoformat())
        self.queue.entries["klukkan_er"] = Entry(["klukkan_er"], "Klukkan er {hour}.", "Klukkan er tvö.", 4, due=D.isoformat())

        async def fake_cli(cfg, args, on_progress=None):
            return 0, json.dumps({
                "gott": {"prompt": "「良い」(状況0)", "answer": "gott"},
                "sofa": {"prompt": "「寝る」", "answer": "sofa"},
                "klukkan_er": {"prompt": "「2時です」", "answer": "Klukkan er tvö."},
            }), ""

        self.refresh(fake_cli)
        self.assertEqual(self.queue.entries["gott"].prompt, "「良い」と言ってください。")
        self.assertEqual(self.queue.entries["sofa"].prompt, "How do you say: Sleep.", "a stale bare question waits for cues")
        self.assertEqual(self.queue.entries["klukkan_er"].prompt, "「2時です」")

    def test_the_stored_wording_stays_when_the_cli_fails(self):
        async def failing(cfg, args, on_progress=None):
            return 2, "", "error: invalid choice: 'questions'"

        async def garbage(cfg, args, on_progress=None):
            return 0, "not json", ""

        for fake in (failing, garbage):
            self.refresh(fake)
            self.assertEqual(self.queue.entries["sofa"].prompt, "How do you say: Sleep.")

    def test_a_stored_template_is_fixed_by_the_refresh(self):
        self.queue.entries["klukkan_er"] = Entry(["klukkan_er"], "Klukkan er {hour}.", "Klukkan er tvö.", 4, due=D.isoformat())
        self.assertNotIn("klukkan_er", self.queue.select(D, 10))

        async def fake_cli(cfg, args, on_progress=None):
            return 0, json.dumps({"klukkan_er": {"prompt": "「今は2時です」と言ってください。", "answer": "Klukkan er tvö."}}), ""

        self.refresh(fake_cli)
        self.assertIn("klukkan_er", self.queue.select(D, 10))


PLAN_CODE = """
import json, sys
from datetime import date
from audiolesson.content import load_curriculum
from audiolesson.cli import _plan
from audiolesson.learner import ItemState, LearnerState
from audiolesson.script import Script

cur = load_curriculum(sys.argv[1], known_lang="ja")
lesson, learner_path = int(sys.argv[2]), sys.argv[3]
open_items, not_fitted = sys.argv[4].split(","), sys.argv[5].split(",")
open_items, not_fitted = [i for i in open_items if i], [i for i in not_fitted if i]
learner = LearnerState.load(learner_path) if len(sys.argv) > 6 else LearnerState("is", "ja", "A1")
if len(sys.argv) <= 6:
    for i in open_items + not_fitted:
        learner.items[i] = ItemState(due="2026-09-20", successes=2, last_outcome="not_recalled", failures=1)
    learner.save(learner_path)
sc = Script(lesson, "t", "is", "ja")
sc.meta.update(new_items=["godan_daginn"] if lesson == 2 else [], open_items=open_items, open_not_fitted=not_fitted)
print(json.dumps(_plan(sc, cur), ensure_ascii=False))
"""


class OpenItemsEndToEndTests(unittest.TestCase):
    """#76: the plan the audio side writes, the queue the bot builds from it, the review and the report back,
    through the pinned audiolesson (no Discord, no paid service): every open item is asked, a failure stays open, a recall closes it."""

    def plan(self, cfg, lesson, open_items, not_fitted, first=False):
        args = [cfg.python, "-c", PLAN_CODE, cfg.curriculum, str(lesson), str(cfg.learner_path("yuki")), ",".join(open_items), ",".join(not_fitted)]
        if not first:
            args.append("again")
        out = subprocess.run(args, cwd=cfg.lla_dir, capture_output=True, text=True, check=True)
        plan = json.loads(out.stdout)  # what plan.json holds, as the bot reads it
        if lesson == 2:  # the lesson's own question for its new item
            plan["review"].append({"items": ["godan_daginn"], "prompt": "「こんにちは」と言って", "answer": "Góðan daginn."})
        return plan

    def outcome(self, cfg, item):
        raw = json.loads(cfg.learner_path("yuki").read_text("utf-8"))
        return raw["items"][item]["last_outcome"]

    def test_open_items_are_asked_reported_to_their_lesson_and_closed_by_a_recall(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = LessonConfig(root=Path(td), users={1: "yuki"})
            cfg.user_dir("yuki").mkdir(parents=True)
            lessons = Lessons(cfg)
            # lesson 1 asked «takk» and «fara_heim» a while ago; their dates are far ahead and «takk» has no written question now
            queue = ReviewQueue()
            old = (D + timedelta(days=30)).isoformat()
            queue.entries["takk"] = Entry(["takk"], "old wording", "Takk.", 1, state="ok", due=old, reviews=3)
            queue.entries["fara_heim"] = Entry(["fara_heim"], "家へ", "Heim.", 1, state="ok", due=old, reviews=3)
            plan = self.plan(cfg, 2, ["takk"], ["fara_heim"], first=True)
            self.assertEqual((plan["open_items"], plan["open_not_fitted"]), (["takk"], ["fara_heim"]))
            queue.add_from_plan(json.loads(json.dumps(plan)), D)
            self.assertNotEqual(queue.entries["takk"].prompt, "old wording", "the serializer's question for «takk» rewords it")
            self.assertEqual(queue.entries["takk"].source_lesson, 1)
            path = cfg.pending_path("yuki")
            queue.save(path)

            day = D + timedelta(days=1)
            queue = ReviewQueue.load(path, day)
            keys = queue.select(day, limit=1)  # a small limit does not drop the required questions
            self.assertEqual(sorted(keys), ["fara_heim", "godan_daginn", "takk"])
            session = ReviewSession(queue, keys, path, day)
            for key in keys:
                session.rate("failed" if key == "takk" else "ok")
            sent = []

            async def real_cli(cfg_, args, on_progress=None):
                sent.append(args)
                return await run_cli(cfg_, args, on_progress)

            with mock.patch("src.lesson.run_cli", real_cli):
                self.assertTrue(asyncio.run(lessons.flush_reports(FakeChannel(), "yuki", queue, path)))
            lesson_flags = sorted(a[a.index("--lesson") + 1] for a in sent)
            self.assertEqual(lesson_flags, ["1", "2"], "a report goes to the lesson that asked the question")
            takk_report = next(a for a in sent if "--failed" in a)
            self.assertEqual((takk_report[takk_report.index("--lesson") + 1], takk_report[takk_report.index("--failed") + 1]), ("1", "takk"))
            self.assertEqual(self.outcome(cfg, "takk"), "not_recalled", "a failure stays open")
            self.assertEqual(self.outcome(cfg, "fara_heim"), "recalled", "a recall closes it")

            # the next plan still lists «takk»; it is asked again, and a recall closes it
            plan = self.plan(cfg, 3, ["takk"], [])
            queue.add_from_plan(json.loads(json.dumps(plan)), day)
            queue.save(path)
            third = day + timedelta(days=1)
            keys = queue.select(third, limit=1)
            self.assertEqual(keys, ["takk"])
            ReviewSession(queue, keys, path, third).rate("ok")
            with mock.patch("src.lesson.run_cli", real_cli):
                self.assertTrue(asyncio.run(lessons.flush_reports(FakeChannel(), "yuki", queue, path)))
            self.assertEqual(self.outcome(cfg, "takk"), "recalled")
            plan = self.plan(cfg, 4, [], [])
            queue.add_from_plan(json.loads(json.dumps(plan)), third)
            self.assertFalse(any(e.open for e in queue.entries.values()), "a closed item is not asked again")

    def test_without_the_open_lists_the_small_limit_drops_them(self):
        """The test fails if either field is removed from the plan: the guard on the guard."""
        with tempfile.TemporaryDirectory() as td:
            cfg = LessonConfig(root=Path(td), users={1: "yuki"})
            cfg.user_dir("yuki").mkdir(parents=True)
            plan = self.plan(cfg, 2, ["takk"], ["fara_heim"], first=True)
            for field in ("open_items", "open_not_fitted"):
                queue = ReviewQueue()
                old = (D + timedelta(days=30)).isoformat()
                for k in ("takk", "fara_heim"):
                    queue.entries[k] = Entry([k], "p", "a", 1, state="ok", due=old)
                stripped = {**plan, field: []}
                queue.add_from_plan(stripped, D)
                keys = queue.select(D + timedelta(days=1), limit=1)
                self.assertNotEqual(sorted(keys), ["fara_heim", "godan_daginn", "takk"], field)


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
