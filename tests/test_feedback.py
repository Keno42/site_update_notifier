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
            "kind": "early_last_appearance",
            "items": ["takk_fyrir"],
            "last_s": 600,
            "end_s": 1800,
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
            self.assertEqual((record.lesson, record.id), (12, "lesson-012.2"))
            self.assertEqual(record.title, "レッスン 12（再生成 2 回目）")
            for ref in ("lesson-012", "12.1", "012.1"):
                self.assertEqual(ledger.load(ref).id, "lesson-012", ref)
            self.assertEqual(ledger.load("12.2").id, "lesson-012.2")
            self.assertEqual(ledger.siblings(record), ["lesson-012", "lesson-012.2"])
            self.assertIsNone(ledger.load(11))
            self.assertIsNone(ledger.load("12.3"))
            with self.assertRaises(ValueError):
                ledger.load("twelve")

    def test_feedback_is_appended_only_and_survives_a_cut_line(self):
        """PR #46 review: a line cut off by a power loss must cost only itself — not
        the next good submission appended after it, and not the whole file when the cut
        falls inside a multi-byte character."""
        with tempfile.TemporaryDirectory() as td:
            ledger = Ledger(Path(td))
            path = Path(td) / FEEDBACK_FILE
            ledger.append({"manifest": "lesson-001", "load": "right", "note": "ok"})
            first = path.read_bytes()
            cut = json.dumps(
                {"manifest": "lesson-002", "note": "þreytt"}, ensure_ascii=False
            )
            raw = cut.encode("utf-8")
            with open(path, "ab") as f:
                f.write(raw[: raw.index("þ".encode()) + 1])  # half of «þ»
            ledger.append({"manifest": "lesson-002", "load": "heavy", "note": "重い"})
            self.assertTrue(path.read_bytes().startswith(first), "nothing rewritten")
            self.assertEqual([e["load"] for e in ledger.events()], ["right", "heavy"])
            ledger.append({"manifest": "lesson-002", "load": "light"})
            self.assertEqual(
                [e["load"] for e in ledger.events("lesson-002")], ["heavy", "light"]
            )

    def test_export_bundles_feedback_and_the_lesson_record(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = saved(Path(td))
            ledger.append({"manifest": "lesson-012", "load": "right"})
            ledger.append({"manifest": "lesson-003", "load": "heavy"})
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
    def test_candidates_leave_out_repeated_situations(self):
        """Older plans list the same situation asked twice; hearing an item in its scene
        again is practice, so the card doesn't ask about it."""
        with tempfile.TemporaryDirectory() as td:
            record = saved(Path(td)).load()
            assert record is not None
            described = [record.describe(c) for c in record.candidates()]
            self.assertEqual(
                described,
                [
                    "後半に出てこなかった気がする: Takk fyrir {thing}.（最後に出たのは約10分ごろ。全30分中）",
                    "終わり近くに、ヒントなしで言う場面がなかった: Ég var að {inf}.",
                ],
            )
            text = form_text(record)
            self.assertIn("Takk fyrir {thing}.（〜をありがとう。）", text)
            self.assertNotIn("同じ場面", text)

    def test_event_ties_answers_to_the_lesson_record(self):
        with tempfile.TemporaryDirectory() as td:
            record = saved(Path(td)).load()
            assert record is not None
            answers = Answers(
                unheard=["takk_fyrir"],
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
            self.assertEqual(len(e["candidates_shown"]), 2)
            text = report_text(record, [e], ["lesson-012"])
            self.assertIn("負荷: ちょうどいい", text)
            self.assertIn("出てこなかった・聞こえなかった: Takk fyrir {thing}.", text)
            self.assertNotIn("使えそう", text)
            self.assertIn(
                "当てはまった候補: 終わり近くに、ヒントなしで言う場面がなかった: Ég var að {inf}.",
                text,
            )
            self.assertIn("気になった点: 同じ表現がくり返し出すぎた", text)
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


def interaction(log, user=1, channel=5):
    return SimpleNamespace(
        user=SimpleNamespace(id=user), channel_id=channel, response=FakeResponse(log)
    )


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
                unheard, sooner, load, concerns = selects
                unheard._values = ["takk_fyrir"]
                await unheard.callback(interaction(log))
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
            self.assertEqual(answers.unheard, ["takk_fyrir"])
            self.assertEqual(answers.sooner, [])
            self.assertEqual(answers.load, "heavy")
            self.assertEqual(answers.concerns, ["c0", "f:pacing"])
            self.assertEqual(answers.note, "例文がほしい")

    def test_post_button_survives_a_restart_and_is_only_for_its_learner(self):
        opened = []

        class Handler:
            async def open_form(self, interaction, ref):
                opened.append((interaction.user.id, ref))

        async def run():
            view = feedback_view(1, "lesson-012.2")
            (button,) = view.children
            self.assertEqual(button.custom_id, "lla-feedback:1:lesson-012.2")
            self.assertTrue(view.is_persistent())
            match = FeedbackButton.__discord_ui_compiled_template__.fullmatch(
                "lla-feedback:1:lesson-012.2"
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
        self.assertEqual(opened, [(1, "lesson-012.2")])


def submit_form(test, log, kw, load="right"):
    """Pick the load in a form opened with ``kw`` and send it."""

    async def run():
        view = kw["view"]
        select = [c for c in view.children if hasattr(c, "options")][2]
        select._values = [load]
        await select.callback(interaction(log))
        send = [c for c in view.children if getattr(c, "label", "") == "送信"]
        await send[0].callback(interaction(log))

    return run()


class EntryPointTests(unittest.TestCase):
    def feedback(self, td, channel_id=0):
        return Feedback({1: "yuki"}, lambda n: Path(td) / n, channel_id, lambda: NOW)

    def test_form_report_and_export_through_discord(self):
        with tempfile.TemporaryDirectory() as td:
            saved(Path(td))
            fb = self.feedback(td)

            async def run():
                log = []
                await fb.open_form(interaction(log, user=9))
                self.assertIn("登録されたユーザーだけ", log[-1][1])
                await fb.open_form(interaction(log), "3")
                self.assertIn("レッスン 3 の記録がありません", log[-1][1])
                await fb.open_form(interaction(log), "twelve")
                self.assertIn("12.2", log[-1][1])
                await fb.report(interaction(log))
                self.assertIn("まだありません", log[-1][1])
                await fb.open_form(interaction(log))
                _, text, kw = log[-1]
                self.assertTrue(kw["ephemeral"])
                await submit_form(self, log, kw)
                await fb.report(interaction(log), "12")
                self.assertIn("負荷: ちょうどいい", log[-1][1])
                self.assertFalse(log[-1][2].get("ephemeral"), "the report is posted")
                await fb.export(interaction(log))
                return log[-1]

            _, text, kw = asyncio.run(run())
            self.assertIn("レッスン 12（lesson-012）", text)
            self.assertEqual(kw["file"].filename, "lesson-012-feedback.zip")
            self.assertTrue(kw["ephemeral"], "learner state and notes stay private")
            events = Ledger(Path(td) / "yuki").events("lesson-012")
            self.assertEqual([e["load"] for e in events], ["right"])

    def test_an_old_post_stays_tied_to_its_own_record_after_a_regeneration(self):
        """PR #46 review: lesson 12 generated again is kept as lesson-012.2; the old
        post's button (and «12.1») must still open and record lesson-012, not the newer
        one."""
        with tempfile.TemporaryDirectory() as td:
            ledger = saved(Path(td))
            regenerated = dict(PLAN, new_items=[PLAN["new_items"][1]])
            write_lesson(Path(td) / "work", regenerated)
            ledger.save_manifest(
                Path(td) / "work", regenerated, None, Path(td) / "x", {}, [], NOW
            )
            fb = self.feedback(td)

            async def run():
                log = []
                await fb.open_form(interaction(log), "lesson-012")  # the old button
                _, text, kw = log[-1]
                self.assertIn("Takk fyrir {thing}.", text, "the old lesson's items")
                await submit_form(self, log, kw, load="heavy")
                await fb.open_form(interaction(log))  # latest = the regenerated one
                _, text, kw = log[-1]
                self.assertIn("再生成 2 回目", text)
                self.assertNotIn("Takk fyrir {thing}.", text)
                await submit_form(self, log, kw, load="light")
                await fb.report(interaction(log), "12")
                latest = log[-1][1]
                await fb.report(interaction(log), "12.1")
                return latest, log[-1][1]

            latest, old = asyncio.run(run())
            self.assertIn("負荷: 軽い", latest)
            self.assertIn("同じ番号の別の記録: lesson-012", latest)
            self.assertIn("負荷: 重い", old)
            self.assertIn("同じ番号の別の記録: lesson-012.2", old)
            by_record = {e["manifest"]: e["load"] for e in ledger.events()}
            self.assertEqual(
                by_record, {"lesson-012": "heavy", "lesson-012.2": "light"}
            )
            with zipfile.ZipFile(ledger.export("12.1", Path(td))) as z:
                lines = z.read("feedback.jsonl").decode().splitlines()
                self.assertIn("lesson-012/manifest.json", z.namelist())
            self.assertEqual([json.loads(x)["load"] for x in lines], ["heavy"])

    def test_the_lesson_channel_applies_to_feedback_too(self):
        """PR #46 review: like /lesson, LESSON_CHANNEL_ID limits feedback commands."""
        with tempfile.TemporaryDirectory() as td:
            saved(Path(td))
            fb = self.feedback(td, channel_id=5)

            async def run():
                log = []
                for call in (fb.open_form, fb.report, fb.export):
                    await call(interaction(log, channel=6))
                    self.assertIn("<#5> で実行してください", log[-1][1])
                    self.assertTrue(log[-1][2]["ephemeral"])
                await fb.report(interaction(log, channel=5))
                return log[-1][1]

            self.assertIn("まだありません", asyncio.run(run()))


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
            # language-learning-audio #130: the form's candidates come from the plan
            self.assertIsInstance(second.plan["review_candidates"], list)
            known = ("後半に出てこなかった気がする", "終わり近くに、ヒントなしで")
            for c in second.candidates():
                self.assertTrue(second.describe(c).startswith(known), c)
            self.assertIn("generate", second.manifest["generate_args"])
            self.assertEqual(
                [v.children[0].custom_id for v in channel.views if v is not None],
                ["lla-feedback:1:lesson-001", "lla-feedback:1:lesson-002"],
            )
            self.assertEqual(list(cfg.work_dir("yuki").iterdir()), [], "work cleaned")


if __name__ == "__main__":
    unittest.main()
