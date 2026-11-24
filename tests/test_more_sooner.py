"""language-learning-audio #585 (lesson 65: «húfu» did not come at once in the review, and the form could not say so): the form's select holds
25 options (Discord), and a 30-minute lesson asks two or three times that. «他の表現を追加» opens the rest of the lesson's asked items, 25 a page,
in the order the lesson first asked them, each with its time; what is chosen there joins the form's «練習が足りなかった・言えなかった表現»."""
import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.feedback import FeedbackView, Ledger, MoreSoonerView, build_event
from tests.test_feedback import interaction

NOW = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)


def plan(asked: int) -> dict:
    new = [{"id": "nyr1", "target": "Nýr.", "meaning": "新しい"}, {"id": "nyr2", "target": "Nýrri.", "meaning": "もっと新しい"}]
    items = [{"id": f"r{k:02d}", "target": f"Orð {k}.", "meaning": f"言葉 {k}"} for k in range(asked)]
    exercises = [{"index": k, "kind": "recall", "stage": "meaning", "items": [f"r{k:02d}"], "label": "", "start_s": 60.0 + 20 * k, "duration_s": 10}
                 for k in range(asked)]
    exercises.append({"index": asked, "kind": "recall", "stage": "meaning", "items": ["r00"], "label": "", "start_s": 9999.0, "duration_s": 10})
    return {"lesson_number": 65, "new_items": new, "reviewed_items": [], "exercise_items": items, "exercises": exercises}


def record_of(td: Path, p: dict):
    work = td / "work"
    work.mkdir(parents=True)
    (work / "lesson-065.plan.json").write_text(json.dumps(p), "utf-8")
    (work / "lesson-065.script.json").write_text('{"segments": []}', "utf-8")
    (work / "lesson-065.transcript.md").write_text("# Lesson 65", "utf-8")
    learner = td / "yuki" / "learner.json"
    learner.parent.mkdir(parents=True)
    learner.write_text("{}", "utf-8")
    ledger = Ledger(td / "yuki")
    ledger.save_manifest(work, p, b"{}", learner, {"bot": "a" * 40, "lla": "b" * 40}, ["generate"], NOW)
    return ledger.load()


class MoreSoonerTests(unittest.TestCase):
    def test_the_rest_of_the_lesson_is_offered_25_a_page_in_its_order_and_joins_the_form(self):
        with tempfile.TemporaryDirectory() as td:
            record = record_of(Path(td), plan(60))

            async def run():
                view = FeedbackView(record, 1, lambda a: None)  # type: ignore[arg-type,return-value]
                sooner = next(c for c in view.children if hasattr(c, "options") and "練習が足りなかった" in (c.placeholder or ""))
                shown = {o.value for o in sooner.options}
                self.assertEqual(len(shown), 25, "the select is full: 2 new, 23 reviewed")
                buttons = {c.label: c for c in view.children if hasattr(c, "label") and c.label}
                more = buttons["他の表現を追加（37件）"]
                self.assertEqual(more.row, 4, "on the form's button row: the five rows stay as they were")
                log = []
                await more.callback(interaction(log))
                kind, text, kw = log[-1]
                picker = kw["view"]
                self.assertIsInstance(picker, MoreSoonerView)
                self.assertTrue(kw["ephemeral"])
                self.assertIn("1/2 ページ、全 37 件", text)
                ids = [i["id"] for i, _ in picker.items]
                self.assertEqual(ids, [f"r{k:02d}" for k in range(23, 60)], "the ones the select could not hold, in the lesson's order")
                self.assertFalse(shown & set(ids))
                self.assertEqual(picker.select.options[0].description, "復習 8:40 · 言葉 23", "the time it was first asked, and its meaning")
                # a choice on page 1, the next page, a choice there, back: page 1's choice is still ticked
                picker.select._values = ["r23", "r30"]
                await picker.select.callback(interaction(log))
                nav = {c.label: c for c in picker.children if hasattr(c, "label") and c.label}
                self.assertTrue(nav["◀ 前の25件"].disabled)
                await nav["次の25件 ▶"].callback(interaction(log))
                self.assertIn("2/2 ページ", log[-1][1])
                self.assertEqual(len(picker.select.options), 12)
                picker.select._values = ["r59"]
                await picker.select.callback(interaction(log))
                self.assertIn("選んでいる表現: 3 件", log[-1][1])
                nav = {c.label: c for c in picker.children if hasattr(c, "label") and c.label}
                self.assertTrue(nav["次の25件 ▶"].disabled)
                await nav["◀ 前の25件"].callback(interaction(log))
                self.assertEqual({o.value for o in picker.select.options if o.default}, {"r23", "r30"})
                # the form's own select, then «追加»: both kept, whichever comes first
                sooner._values = ["nyr1"]
                await sooner.callback(interaction(log))
                await {c.label: c for c in picker.children if hasattr(c, "label") and c.label}["追加"].callback(interaction(log))
                self.assertIn("3 件をフォームに加えました", log[-1][1])
                self.assertEqual(view.answers.sooner, ["nyr1", "r23", "r30", "r59"])
                sooner._values = ["nyr2"]
                await sooner.callback(interaction(log))
                self.assertEqual(view.answers.sooner, ["nyr2", "r23", "r30", "r59"], "a later change of the select keeps the additions")
                view.answers.load = "right"
                event = build_event(record, view.answers, "yuki", NOW)
                self.assertEqual(event["sooner"], ["nyr2", "r23", "r30", "r59"], "one list for the pace and the schedule")
                # opened again: the additions are ticked, and choosing none removes them
                await more.callback(interaction(log))
                again = log[-1][2]["view"]
                self.assertEqual({o.value for o in again.select.options if o.default}, {"r23", "r30"})
                again.chosen = []
                await {c.label: c for c in again.children if hasattr(c, "label") and c.label}["追加"].callback(interaction(log))
                self.assertEqual(view.answers.sooner, ["nyr2"])

            asyncio.run(run())

    def test_a_picker_left_open_adds_nothing_once_the_form_is_sent(self):
        """Review of #240: open «他の表現を追加», go back and press «送信», then «追加» in the picker. The event was recorded without the additions;
        the picker must not say they were added, nor change the sent answers."""
        with tempfile.TemporaryDirectory() as td:
            record = record_of(Path(td), plan(60))
            submitted = []

            async def submit(answers):
                submitted.append(list(answers.sooner))

            async def run():
                view = FeedbackView(record, 1, submit)
                buttons = {c.label: c for c in view.children if hasattr(c, "label") and c.label}
                log = []
                await buttons["他の表現を追加（37件）"].callback(interaction(log))
                picker = log[-1][2]["view"]
                picker.select._values = ["r23"]
                await picker.select.callback(interaction(log))
                view.answers.load = "right"
                await buttons["送信"].callback(interaction(log))
                self.assertEqual(submitted, [[]], "sent before «追加»: nothing added")
                add = {c.label: c for c in picker.children if hasattr(c, "label") and c.label}["追加"]
                self.assertFalse(await picker.interaction_check(interaction(log)), "the picker refuses once the form is sent")
                self.assertEqual(log[-1][0], "edit")
                self.assertIn("もう送信されたため", log[-1][1])
                self.assertIsNone(log[-1][2]["view"], "the picker closes")
                self.assertNotIn("フォームに加えました", " ".join(str(t) for _, t, _ in log))
                self.assertEqual(view.answers.sooner, [], "the sent answers are unchanged")
                self.assertTrue(add)

            asyncio.run(run())

    def test_no_button_when_the_select_holds_every_asked_item(self):
        with tempfile.TemporaryDirectory() as td:
            record = record_of(Path(td), plan(20))

            async def run():
                view = FeedbackView(record, 1, lambda a: None)  # type: ignore[arg-type,return-value]
                self.assertFalse([c for c in view.children if "他の表現を追加" in (getattr(c, "label", "") or "")])

            asyncio.run(run())

    def test_only_the_lesson_s_learner_can_choose(self):
        with tempfile.TemporaryDirectory() as td:
            record = record_of(Path(td), plan(40))

            async def run():
                view = FeedbackView(record, 1, lambda a: None)  # type: ignore[arg-type,return-value]
                picker = MoreSoonerView(view)
                log = []
                self.assertFalse(await picker.interaction_check(interaction(log, user=2)))

            asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
