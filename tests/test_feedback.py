"""src/feedback.py のテスト. 実行: python -m unittest discover -s tests -t ."""

import asyncio
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from src.feedback import (
    FEEDBACK_FILE,
    Answers,
    Feedback,
    FeedbackButton,
    FeedbackView,
    Ledger,
    build_event,
    feedback_view,
    form_text,
    report_text,
)
from src.lesson import LLA_DIR, LessonConfig, Lessons

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 9, 29, 21, 10, tzinfo=JST)

PLAN = {
    "lesson_number": 12,
    "new_items": [
        {
            "id": "takk_fyrir",
            "target": "Takk fyrir {thing}.",
            "meaning": "〜をありがとう。",
        },
        {
            "id": "eg_var_ad_inf",
            "target": "Ég var að {inf}.",
            "meaning": "ちょうど〜していたところです。",
        },
    ],
    "reviewed_items": [{"id": "takk", "target": "Takk.", "meaning": "ありがとう。"}],
    "review_candidates": [
        {"kind": "repeated_situation", "items": ["takk"], "count": 2, "prompt": "…"},
        {
            "kind": "repeated_situation",
            "items": ["takk_fyrir"],
            "count": 3,
            "prompt": "…",
        },
        {
            "kind": "no_late_recall",
            "items": ["eg_var_ad_inf"],
            "last_recall_s": 400,
            "end_s": 1800,
        },
    ],
}


def write_lesson(work: Path, plan=PLAN) -> None:
    work.mkdir(parents=True, exist_ok=True)
    stem = f"lesson-{plan['lesson_number']:03d}"
    (work / f"{stem}.plan.json").write_text(json.dumps(plan), "utf-8")
    (work / f"{stem}.script.json").write_text('{"segments": []}', "utf-8")
    (work / f"{stem}.transcript.md").write_text("# Lesson 12", "utf-8")


def saved(root: Path) -> Ledger:
    ledger = Ledger(root / "yuki")
    write_lesson(root / "work")
    learner = root / "yuki" / "learner.json"
    learner.parent.mkdir(parents=True, exist_ok=True)
    learner.write_text('{"after": true}', "utf-8")
    ledger.save_manifest(
        root / "work", PLAN, b'{"before": true}', learner,
        {"bot": "a" * 40, "lla": "b" * 40}, ["generate"], NOW,
    )  # fmt: skip
    return ledger


class LedgerTests(unittest.TestCase):
    def test_manifest_keeps_the_lesson_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = saved(Path(td))
            d = ledger.manifest_dir(12)
            assert d is not None
            self.assertEqual(d.name, "lesson-012")
            m = json.loads((d / "manifest.json").read_text("utf-8"))
            self.assertEqual(m["revisions"], {"bot": "a" * 40, "lla": "b" * 40})
            self.assertEqual(
                set(m["files"]),
                {
                    "lesson-012.plan.json",
                    "lesson-012.script.json",
                    "lesson-012.transcript.md",
                    "learner.before.json",
                },
            )
            self.assertEqual(
                (d / "learner.before.json").read_text(), '{"before": true}'
            )
            self.assertIsNotNone(m["learner_after_sha256"])
            # the same number again (a regenerated lesson) gets its own record
            ledger.save_manifest(
                Path(td) / "work", PLAN, None, Path(td) / "none", {}, [], NOW
            )
            self.assertEqual(ledger.manifest_dir(12).name, "lesson-012.2")
            self.assertTrue((d / "learner.before.json").exists(), "first one untouched")
            record = ledger.load()
            assert record is not None
            self.assertEqual(record.lesson, 12)
            self.assertIsNone(ledger.load(11))

    def test_feedback_is_appended_only(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Ledger(Path(td))
            ledger.append({"lesson": 1, "load": "right"})
            first = (Path(td) / FEEDBACK_FILE).read_text("utf-8")
            with open(Path(td) / FEEDBACK_FILE, "a") as f:
                f.write('{"lesson": 2, "lo')  # a line cut off by a power loss
            ledger.append({"lesson": 2, "load": "heavy"})
            self.assertTrue(
                (Path(td) / FEEDBACK_FILE).read_text("utf-8").startswith(first)
            )
            self.assertEqual([e["load"] for e in ledger.events()], ["right"])
            ledger.append({"lesson": 2, "load": "light"})
            self.assertEqual([e["load"] for e in ledger.events(2)], ["light"])

    def test_export_bundles_feedback_and_the_lesson_record(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = saved(Path(td))
            ledger.append({"lesson": 12, "load": "right"})
            ledger.append({"lesson": 3, "load": "heavy"})
            out = ledger.export(12, Path(td))
            assert out is not None
            with zipfile.ZipFile(out) as z:
                names = set(z.namelist())
                lines = z.read("feedback.jsonl").decode().splitlines()
            self.assertIn("lesson-012/manifest.json", names)
            self.assertIn("lesson-012/lesson-012.transcript.md", names)
            self.assertIn("lesson-012/learner.before.json", names)
            self.assertEqual([json.loads(x)["load"] for x in lines], ["right"])
            self.assertIsNone(ledger.export(99, Path(td)))


class FormTests(unittest.TestCase):
    def test_candidates_are_described_most_repeated_first(self):
        with tempfile.TemporaryDirectory() as td:
            record = saved(Path(td)).load()
            assert record is not None
            described = [record.describe(c) for c in record.candidates()]
            self.assertEqual(
                described,
                [
                    "終盤にヒントなしで言う機会がない: Ég var að {inf}.",
                    "同じ場面が3回: Takk fyrir {thing}.",
                    "同じ場面が2回: Takk.",
                ],
            )
            text = form_text(record)
            self.assertIn("Takk fyrir {thing}.（〜をありがとう。）", text)
            self.assertIn("・同じ場面が3回", text)

    def test_event_ties_answers_to_the_lesson_record(self):
        with tempfile.TemporaryDirectory() as td:
            record = saved(Path(td)).load()
            assert record is not None
            answers = Answers(
                usable=["takk_fyrir"],
                sooner=["eg_var_ad_inf"],
                load="right",
                concerns=["c1", "f:repetitive"],
                note="後半が速い",
            )
            e = build_event(record, answers, "yuki", NOW)
            self.assertEqual(e["lesson"], 12)
            self.assertEqual(e["manifest"], "lesson-012")
            self.assertEqual(e["revisions"]["lla"], "b" * 40)
            files = record.manifest["files"]
            self.assertEqual(e["transcript_sha256"], files["lesson-012.transcript.md"])
            self.assertEqual(e["learner_sha256"], files["learner.before.json"])
            self.assertEqual(e["friction"], ["repetitive"])
            self.assertEqual(e["candidates_confirmed"], [record.candidates()[1]])
            self.assertEqual(len(e["candidates_shown"]), 3)
            text = report_text(record, [e], 12)
            self.assertIn("負荷: ちょうどいい", text)
            self.assertIn("使えそう: Takk fyrir {thing}.", text)
            self.assertIn("当てはまった候補: 同じ場面が3回: Takk fyrir {thing}.", text)
            self.assertIn("気になった点: 繰り返しが多い", text)
            self.assertIn("メモ: 後半が速い", text)
            self.assertIn("bot `aaaaaaa`", text)

    def test_a_plan_without_candidates_still_gets_a_form(self):
        """An older language-learning-audio writes no review_candidates."""
        with tempfile.TemporaryDirectory() as td:
            ledger = Ledger(Path(td) / "yuki")
            plan = {k: v for k, v in PLAN.items() if k != "review_candidates"}
            write_lesson(Path(td) / "work", plan)
            ledger.save_manifest(
                Path(td) / "work", plan, None, Path(td) / "x", {}, [], NOW
            )
            record = ledger.load()
            assert record is not None
            self.assertEqual(record.candidates(), [])
            self.assertNotIn("候補", form_text(record))


class FakeResponse:
    def __init__(self, log):
        self.log = log

    async def send_message(self, content=None, **kw):
        self.log.append(("send", content, kw))

    async def edit_message(self, **kw):
        self.log.append(("edit", kw.get("content"), kw))

    async def defer(self):
        self.log.append(("defer", None, {}))

    async def send_modal(self, modal):
        self.log.append(("modal", None, {"modal": modal}))


def interaction(log, user=1):
    return SimpleNamespace(user=SimpleNamespace(id=user), response=FakeResponse(log))


class ViewTests(unittest.TestCase):
    def test_form_needs_only_the_load_and_records_once(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = saved(Path(td))
            record = ledger.load()
            assert record is not None
            submitted = []

            async def submit(answers):
                submitted.append(answers)

            async def run():
                view = FeedbackView(record, 1, submit)
                selects = [c for c in view.children if hasattr(c, "options")]
                self.assertEqual(len(selects), 4)
                buttons = {c.label: c for c in view.children if hasattr(c, "label")}
                log = []
                self.assertFalse(await view.interaction_check(interaction(log, user=2)))
                await buttons["送信"].callback(interaction(log))
                self.assertEqual(submitted, [], "the load is required")
                usable, sooner, load, concerns = selects
                usable._values = ["takk_fyrir"]
                await usable.callback(interaction(log))
                load._values = ["heavy"]
                await load.callback(interaction(log))
                concerns._values = ["c0", "f:pacing"]
                await concerns.callback(interaction(log))
                await buttons["メモを書く"].callback(interaction(log))
                modal = log[-1][2]["modal"]
                modal.note._value = "例文がほしい"
                await modal.on_submit(interaction(log))
                self.assertIn("メモ: 例文がほしい", log[-1][1])
                await buttons["送信"].callback(interaction(log))
                return view

            view = asyncio.run(run())
            self.assertTrue(view.is_finished())
            (answers,) = submitted
            self.assertEqual(answers.usable, ["takk_fyrir"])
            self.assertEqual(answers.sooner, [])
            self.assertEqual(answers.load, "heavy")
            self.assertEqual(answers.concerns, ["c0", "f:pacing"])
            self.assertEqual(answers.note, "例文がほしい")

    def test_post_button_survives_a_restart_and_is_only_for_its_learner(self):
        opened = []

        class Handler:
            async def open_form(self, interaction, lesson):
                opened.append((interaction.user.id, lesson))

        async def run():
            view = feedback_view(1, 12)
            (button,) = view.children
            self.assertEqual(button.custom_id, "lla-feedback:1:12")
            self.assertTrue(view.is_persistent())
            match = FeedbackButton.__discord_ui_compiled_template__.fullmatch(
                "lla-feedback:1:12"
            )
            rebuilt = await FeedbackButton.from_custom_id(None, button.item, match)
            FeedbackButton.handler = Handler()
            try:
                log = []
                await rebuilt.callback(interaction(log, user=2))
                self.assertIn("このレッスンを受けた人だけ", log[-1][1])
                await rebuilt.callback(interaction(log, user=1))
            finally:
                FeedbackButton.handler = None

        asyncio.run(run())
        self.assertEqual(opened, [(1, 12)])


class EntryPointTests(unittest.TestCase):
    def test_form_report_and_export_through_discord(self):
        with tempfile.TemporaryDirectory() as td:
            saved(Path(td))
            fb = Feedback({1: "yuki"}, lambda n: Path(td) / n, now=lambda: NOW)

            async def run():
                log = []
                await fb.open_form(interaction(log, user=9))
                self.assertIn("登録されたユーザーだけ", log[-1][1])
                await fb.open_form(interaction(log), 3)
                self.assertIn("レッスン 3の記録がありません", log[-1][1])
                await fb.report(interaction(log))
                self.assertIn("まだありません", log[-1][1])
                await fb.open_form(interaction(log))
                _, text, kw = log[-1]
                self.assertTrue(kw["ephemeral"])
                view = kw["view"]
                load = [c for c in view.children if hasattr(c, "options")][2]
                load._values = ["right"]
                await load.callback(interaction(log))
                send = [c for c in view.children if getattr(c, "label", "") == "送信"]
                await send[0].callback(interaction(log))
                await fb.report(interaction(log))
                self.assertIn("負荷: ちょうどいい", log[-1][1])
                await fb.export(interaction(log), tmp=Path(td) / "tmp")
                return log[-1]

            _, text, kw = asyncio.run(run())
            self.assertIn("レッスン 12", text)
            self.assertEqual(kw["file"].filename, "lesson-012-feedback.zip")
            self.assertEqual(list((Path(td) / "tmp").iterdir()), [], "zip removed")
            events = Ledger(Path(td) / "yuki").events(12)
            self.assertEqual([e["load"] for e in events], ["right"])


@unittest.skipUnless((LLA_DIR / "audiolesson").exists(), "submodule not checked out")
class GenerationTests(unittest.TestCase):
    def test_a_generated_lesson_is_recorded_and_posted_with_the_button(self):
        from tests.test_lesson import FakeChannel

        with tempfile.TemporaryDirectory() as td:
            cfg = LessonConfig(
                root=Path(td), users={1: "yuki"}, minutes=3,
                extra_args=["--provider", "stub"],
            )  # fmt: skip
            lessons = Lessons(cfg)
            channel = FakeChannel()
            asyncio.run(lessons.generate_and_post(channel, "yuki"))
            asyncio.run(lessons.generate_and_post(channel, "yuki"))
            ledger = Ledger(cfg.user_dir("yuki"))
            first, second = ledger.load(1), ledger.load(2)
            assert first is not None and second is not None
            self.assertNotIn(
                "learner.before.json", first.manifest["files"], "no learner yet"
            )
            self.assertEqual(
                second.manifest["files"]["learner.before.json"],
                first.manifest["learner_after_sha256"],
                "lesson 2 was generated from the state lesson 1 left",
            )
            self.assertIn("lesson-002.script.json", second.manifest["files"])
            self.assertIn("generate", second.manifest["generate_args"])
            self.assertEqual(
                [v.children[0].custom_id for v in channel.views if v is not None],
                ["lla-feedback:1:1", "lla-feedback:1:2"],
            )
            self.assertEqual(list(cfg.work_dir("yuki").iterdir()), [], "work cleaned")


if __name__ == "__main__":
    unittest.main()
