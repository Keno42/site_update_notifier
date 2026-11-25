"""language-learning-audio (the owner, 2026-11-26: «I couldn't tell it was to be answered this way»): a question carries the other answers it
accepts (``also``), from the plan, from the queue's refinement and from ``questions``; the revealed answer shows them, so a learner who said
another right line does not mark it as not said."""
import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.review import ReviewSession
from src.review_queue import Entry, ReviewQueue

D = date(2026, 11, 26)
PLAN = {"lesson_number": 70, "new_items": [{"id": "eg_tharf_simkort"}], "review": [
    {"items": ["eg_tharf_simkort"], "prompt": "At the phone shop. Say you need a SIM card.", "answer": "Mig vantar símkort.", "also": ["Ég þarf símkort."]},
    {"items": ["eg_datt"], "prompt": "Say you fell.", "answer": "Ég datt."}]}


class ReviewAlsoTests(unittest.TestCase):
    def test_the_plan_s_other_answers_are_kept_and_shown_with_the_answer(self):
        with tempfile.TemporaryDirectory() as td:
            queue = ReviewQueue()
            queue.add_from_plan(PLAN, D)
            path = Path(td) / "q.json"
            queue.save(path)
            back = ReviewQueue.load(path, D)
            self.assertEqual(back.entries["eg_tharf_simkort"].also, ["Ég þarf símkort."])
            self.assertEqual(back.entries["eg_datt"].also, [], "an older plan's question has none")
            session = ReviewSession(back, ["eg_tharf_simkort"], path, D)
            self.assertNotIn("Ég þarf", session.render(revealed=False))
            self.assertIn("ほかにも正解: **Ég þarf símkort.**", session.render(revealed=True))

    def test_a_question_queued_before_reaches_its_other_answers_through_questions(self):
        queue = ReviewQueue()
        queue.entries["eg_tharf_simkort"] = Entry(items=["eg_tharf_simkort"], prompt="At the phone shop. Say you need a SIM card.",
                                                  answer="Mig vantar símkort.", source_lesson=60, due=D.isoformat())
        queue.refresh_wording({"eg_tharf_simkort": {"prompt": "At the phone shop. Say you need a SIM card.", "answer": "Mig vantar símkort.",
                                                    "cues": ["At the phone shop. Say you need a SIM card."], "also": ["Ég þarf símkort."]}})
        self.assertEqual(queue.entries["eg_tharf_simkort"].also, ["Ég þarf símkort."])
        queue.apply_refinement([{"items": ["eg_tharf_simkort"], "prompt": "x", "answer": "Mig vantar símkort.", "also": []}], [], D)
        self.assertEqual(queue.entries["eg_tharf_simkort"].also, [], "the refinement's answer list is the current one")


    def test_a_rewritten_answer_never_keeps_the_old_answer_s_alternatives(self):
        """Review of #246: a rewrite to a question without ``also`` kept the old list, so the old answer's other right line showed as right."""
        def queue():
            q = ReviewQueue()
            q.entries["x"] = Entry(items=["x"], prompt="old", answer="Mig vantar símkort.", source_lesson=60, due=D.isoformat(), also=["Ég þarf símkort."])
            return q
        q = queue()
        q.apply_refinement([{"items": ["x"], "prompt": "new", "answer": "Ég þarf hjálp."}], [], D, rewrite=True)
        self.assertEqual((q.entries["x"].answer, q.entries["x"].also), ("Ég þarf hjálp.", []))
        q = queue()
        q.add_from_plan({"lesson_number": 71, "review": [{"items": ["x"], "prompt": "new", "answer": "Ég þarf hjálp."}]}, D)
        self.assertEqual((q.entries["x"].answer, q.entries["x"].also), ("Ég þarf hjálp.", []))
        q = queue()
        q.entries["x"].prompt = "{stale}"
        q.refresh_wording({"x": {"prompt": "new", "answer": "Ég þarf hjálp.", "cues": ["new"]}})
        self.assertEqual((q.entries["x"].answer, q.entries["x"].also), ("Ég þarf hjálp.", []))
        q = queue()
        q.apply_refinement([{"items": ["x"], "prompt": "old", "answer": "Mig vantar símkort."}], [], D, rewrite=True)
        self.assertEqual(q.entries["x"].also, ["Ég þarf símkort."], "the same answer from a source without the field keeps its list")


if __name__ == "__main__":
    unittest.main()
