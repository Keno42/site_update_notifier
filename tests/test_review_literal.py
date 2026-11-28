"""language-learning-audio #627: a question whose answer's own words differ from its meaning carries them (``literal``: «Ég hef það gott.» is
"I have it good." for "I'm doing well."). The revealed answer shows them; the question does not, since before the answer they would give it away."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.review import ReviewSession
from src.review_queue import Entry, ReviewQueue

D = date(2026, 11, 28)
Q = {"items": ["eg_hef_thad_gott"], "prompt": "A colleague asks how you're doing. Say you're doing well.\nThey say: Hvernig hefurðu það?",
     "answer": "Ég hef það gott.", "literal": "I have it good."}
PLAN = {"lesson_number": 71, "new_items": [{"id": "eg_hef_thad_gott"}], "review": [Q, {"items": ["eg_datt"], "prompt": "Say you fell.", "answer": "Ég datt."}]}


class ReviewLiteralTests(unittest.TestCase):
    def test_kept_and_shown_with_the_answer_only(self):
        with tempfile.TemporaryDirectory() as td:
            queue = ReviewQueue()
            queue.add_from_plan(PLAN, D)
            path = Path(td) / "q.json"
            queue.save(path)
            back = ReviewQueue.load(path, D)
            self.assertEqual(back.entries["eg_hef_thad_gott"].literal, "I have it good.")
            self.assertEqual(back.entries["eg_datt"].literal, "", "a question with none")
            session = ReviewSession(back, ["eg_hef_thad_gott"], path, D)
            self.assertNotIn("I have it good", session.render(revealed=False))
            self.assertIn("直訳: I have it good.", session.render(revealed=True))

    def test_a_question_queued_before_reaches_it_through_questions_and_the_refinement(self):
        queue = ReviewQueue()
        queue.entries["eg_hef_thad_gott"] = Entry(items=["eg_hef_thad_gott"], prompt=Q["prompt"], answer=Q["answer"], source_lesson=60, due=D.isoformat())
        queue.refresh_wording({"eg_hef_thad_gott": {"prompt": Q["prompt"], "answer": Q["answer"], "cues": [Q["prompt"]], "literal": "I have it good."}})
        self.assertEqual(queue.entries["eg_hef_thad_gott"].literal, "I have it good.")
        queue.apply_refinement([{**Q, "literal": "I have it well."}], [], D)
        self.assertEqual(queue.entries["eg_hef_thad_gott"].literal, "I have it well.")

    def test_a_rewritten_answer_never_keeps_the_old_answer_s_words(self):
        q = ReviewQueue()
        q.entries["x"] = Entry(items=["x"], prompt="old", answer="Ég hef það gott.", source_lesson=60, due=D.isoformat(), literal="I have it good.")
        q.apply_refinement([{"items": ["x"], "prompt": "new", "answer": "Allt gott."}], [], D, rewrite=True)
        self.assertEqual((q.entries["x"].answer, q.entries["x"].literal), ("Allt gott.", ""))


    def test_an_empty_literal_drops_the_stored_one(self):
        """Review of language-learning-audio#640: every question now carries ``literal``; "" (a longer answer, or the literal removed) clears it."""
        q = ReviewQueue()
        q.entries["x"] = Entry(items=["x"], prompt="p", answer="Ég hef það gott.", source_lesson=60, due=D.isoformat(), literal="I have it good.")
        q.apply_refinement([{"items": ["x"], "prompt": "p", "answer": "Ég hef það gott.", "literal": ""}], [], D)
        self.assertEqual(q.entries["x"].literal, "")
        q.entries["x"].literal = "I have it good."
        q.refresh_wording({"x": {"prompt": "p", "answer": "Ég hef það gott.", "cues": ["p"], "literal": ""}})
        self.assertEqual(q.entries["x"].literal, "")



LLA = Path(os.environ.get("LLA_ROOT") or Path(__file__).resolve().parent.parent / "external" / "language-learning-audio")


class QuestionsOutputTests(unittest.TestCase):
    """Review of #255: the refresh reads ``audiolesson questions``'s own output, so an item whose literal was removed (or never had one) must
    clear a stored literal through it, not only through a hand-written ``"literal": ""``."""

    def test_the_current_questions_output_clears_a_stale_literal(self):
        if not (LLA / "audiolesson" / "literal.py").is_file():
            self.skipTest("no language-learning-audio with #640's literal here (external/ or LLA_ROOT)")
        run = subprocess.run([sys.executable, "-m", "audiolesson.cli", "questions", "curricula/is-en", "--known", "en", "--ids", "eg_hef_thad_gott"],
                             cwd=LLA, capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stderr)
        fresh = json.loads(run.stdout)
        self.assertIn("literal", fresh["eg_hef_thad_gott"], "every item carries it, \"\" when it has none: an omitted field cannot clear")
        q = ReviewQueue()
        now = fresh["eg_hef_thad_gott"]
        q.entries["eg_hef_thad_gott"] = Entry(items=["eg_hef_thad_gott"], prompt=now["prompt"], answer=now["answer"], source_lesson=60,
                                              due=D.isoformat(), literal="stale words")
        q.refresh_wording(fresh)
        self.assertEqual(q.entries["eg_hef_thad_gott"].literal, now["literal"], "the stored literal follows the current course's")
        self.assertNotEqual(q.entries["eg_hef_thad_gott"].literal, "stale words")


if __name__ == "__main__":
    unittest.main()
