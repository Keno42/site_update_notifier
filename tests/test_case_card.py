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
