"""language-learning-audio #548 step 2 (the owner, 2026-11-20: the «why» of a form in text, not in the audio): the anchor frames a native teacher
gave, one noun with the small word in front changed, are posted as a card with the day's forms beside them, when a form not shown before is among
the new items; and a grammar explanation (解説を頼む) is told to point to them rather than to case names."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src import feedback
from src.lesson import Lessons
from tests.test_lesson import FakeChannel, LessonConfig

ANCHORS = {"noun": "bankinn", "lead": "前の小さな語で、名詞の形が決まります。", "frames": [
    {"frame": "", "line": "Hér er bankinn.", "meaning": "ここに銀行があります。"}, {"frame": "um", "line": "um bankann", "meaning": "銀行について"},
    {"frame": "frá", "line": "frá bankanum", "meaning": "銀行から"}, {"frame": "til", "line": "til bankans", "meaning": "銀行へ"}]}
PLAN = {"lesson_number": 67, "new_items": [], "case_frames": {"anchors": ANCHORS, "items": {
    "get_eg_fengid_reikninginn": {"target": "Get ég fengið reikninginn?", "meaning": "お会計をお願いできますか？", "like": {"reikninginn": "um"}},
    "eg_er_med_ofnaemi_fyrir_hnetum": {"target": "Ég er með ofnæmi fyrir hnetum.", "meaning": "ナッツのアレルギーがあります。", "like": {"hnetum": "frá"}}}}}


class CaseFrameCardTests(unittest.TestCase):
    def test_the_card_shows_the_frames_and_the_day_s_forms_beside_them(self):
        text, shown = feedback.case_frame_card(PLAN, set())
        self.assertIn("前の小さな語で、名詞の形が決まります。", text)
        self.assertIn("・frá bankanum（銀行から）", text)
        self.assertIn("・Get ég fengið reikninginn? — «reikninginn» は «um bankann» と同じ形", text)
        self.assertIn("«hnetum» は «frá bankanum» と同じ形", text)
        self.assertEqual(shown, ["reikninginn:um", "hnetum:frá"])
        for term in ("対格", "与格", "属格", "accusative", "dative", "genitive"):
            self.assertNotIn(term, text)

    def test_only_forms_not_shown_before_bring_the_card(self):
        text, shown = feedback.case_frame_card(PLAN, {"reikninginn:um"})
        self.assertNotIn("reikninginn", text)
        self.assertEqual(shown, ["hnetum:frá"])
        self.assertIsNone(feedback.case_frame_card(PLAN, {"reikninginn:um", "hnetum:frá"}))
        self.assertIsNone(feedback.case_frame_card({"lesson_number": 1}, set()), "an older plan: no card")

    def test_a_form_its_verb_sets_brings_its_own_line(self):
        """#548: «Fluginu er seinkað.»: no frame explains a form its verb sets; its line («why») is shown once, without the frames."""
        plan = {"lesson_number": 68, "case_frames": {"anchors": ANCHORS, "items": {
            "flugid_er_seinkad": {"target": "Fluginu er seinkað.", "meaning": "The flight is delayed.", "like": {},
                                  "why": "«seinka» puts what is delayed in this form: «flugið» becomes «fluginu»."}}}}
        text, shown = feedback.case_frame_card(plan, set())
        self.assertIn("・Fluginu er seinkað. — «seinka» puts what is delayed in this form", text)
        self.assertNotIn("frá bankanum", text, "no frame to show: the frames stay out")
        self.assertEqual(shown, ["why:flugid_er_seinkad"])
        self.assertIsNone(feedback.case_frame_card(plan, set(shown)))

    def test_the_lesson_posts_it_once_per_form(self):
        with tempfile.TemporaryDirectory() as td:
            lessons = Lessons(LessonConfig(root=Path(td), users={1: "yuki"}, default_minutes=5, default_new_list="after", default_order="spread"))
            first, second = FakeChannel(), FakeChannel()
            asyncio.run(lessons.post_case_frames(first, "yuki", PLAN))
            asyncio.run(lessons.post_case_frames(second, "yuki", PLAN))
            self.assertEqual(len([t for t, _ in first.sent if t and t.startswith("**名詞の形の目安**")]), 1)
            self.assertEqual(second.sent, [], "the same forms: no second card")
            seen = json.loads((Path(td) / "yuki" / feedback.CASE_FRAMES_SEEN).read_text("utf-8"))
            self.assertEqual(sorted(seen), ["hnetum:frá", "reikninginn:um"])

    def test_a_record_that_cannot_be_written_does_not_raise(self):
        """Review of #242: the lesson is posted when the card comes; a failure to write the forms shown must not reach the caller, which still
        has to clear «generating»."""
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            lessons = Lessons(LessonConfig(root=Path(td), users={1: "yuki"}, default_minutes=5, default_new_list="after", default_order="spread"))
            channel = FakeChannel()
            with mock.patch.object(Path, "write_text", side_effect=OSError("read-only")), self.assertLogs(level="ERROR"):
                asyncio.run(lessons.post_case_frames(channel, "yuki", PLAN))
            self.assertEqual(len([t for t, _ in channel.sent if t and t.startswith("**名詞の形の目安**")]), 1, "the card was posted")

    def test_the_lesson_is_cleared_even_when_the_card_fails(self):
        """Review of #242: the lesson is generated and posted, then the card fails; «generating» is cleared all the same, so the next /lesson
        does not take the posted lesson for a failed one."""
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            from tests.test_lesson import EndToEndTests

            cfg = EndToEndTests.config(EndToEndTests(), td)  # the real CLI, stub voices
            lessons = Lessons(cfg)
            channel = FakeChannel()
            with mock.patch.object(Lessons, "post_case_frames", side_effect=RuntimeError("card")), self.assertLogs(level="ERROR"):
                asyncio.run(lessons.generate_and_post(channel, "yuki"))
            self.assertFalse(cfg.generating_path("yuki").exists())
            self.assertTrue(any(t and "フィードバック" in t for t, _ in channel.sent), "the lesson was posted")

    def test_a_grammar_explanation_is_told_to_use_the_frames(self):
        record = SimpleNamespace(plan=PLAN, items=lambda: {"get_eg_fengid_reikninginn": {"id": "get_eg_fengid_reikninginn", "target": "Get ég fengið reikninginn?"}},
                                 segments=lambda: [])
        grammar = feedback.support_messages(record, {"item_id": "get_eg_fengid_reikninginn", "kind": "grammar"})[1]["content"]
        self.assertIn(feedback.CASE_FRAMES_GUIDE, grammar)
        self.assertIn("um bankann（銀行について）", grammar)
        sound = feedback.support_messages(record, {"item_id": "get_eg_fengid_reikninginn", "kind": "pronunciation"})[1]["content"]
        self.assertNotIn(feedback.CASE_FRAMES_GUIDE, sound, "a pronunciation explanation: no frames")


if __name__ == "__main__":
    unittest.main()
