"""A question's key does not depend on the order of its items (language-learning-audio #626).

The plan lists the same two items in the order of the exercise that asked them: a recall of the pattern «erud_thid_med, mjolk», a recall of the
word through its sentence «mjolk, erud_thid_med». Keyed in that order, the next plan's question was a new entry, and the refinement folded the
answered one («言えなかった») into it without its result: the failure was gone from the queue. One key for one set of items keeps it."""

import json
import tempfile
import unittest
from pathlib import Path

from src.review_queue import FORMAT, Entry, ReviewQueue, key_for
from tests.test_review_queue import D, plan

PAIR = ["erud_thid_med", "mjolk"]


class QueueKeyTests(unittest.TestCase):
    def test_the_same_items_in_another_order_are_one_question(self):
        self.assertEqual(key_for(PAIR), key_for(list(reversed(PAIR))))
        q = ReviewQueue()
        q.add_from_plan(plan(1, PAIR, [PAIR]), D)
        (key,) = q.entries
        q.entries[key].state = "failed"
        q.add_from_plan(plan(2, [], [list(reversed(PAIR))]), D)
        self.assertEqual(list(q.entries), [key], "no second entry for the same question")
        self.assertEqual(q.entries[key].state, "failed", "the answer's result is kept")

    def test_a_queue_saved_with_ordered_keys_is_rekeyed_when_read(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "queue.json"
            failed = Entry(items=["mjolk", "erud_thid_med"], prompt="p", answer="Eruð þið með mjólk?", source_lesson=1, state="failed", due=D.isoformat())
            fresh = Entry(items=PAIR, prompt="p", answer="Eruð þið með mjólk?", source_lesson=2, due=D.isoformat())
            bonus = Entry(items=["mjolk", "a"], prompt="p", answer="b", source_lesson=1, bonus=True, due=D.isoformat())
            raw = {"format": FORMAT, "items": {"mjolk+erud_thid_med": failed.__dict__, "erud_thid_med+mjolk": fresh.__dict__,
                                                "bonus:1:mjolk+a": bonus.__dict__}}
            path.write_text(json.dumps(raw), "utf-8")
            q = ReviewQueue.load(path, D)
            self.assertEqual(sorted(q.entries), ["bonus:1:a+mjolk", "erud_thid_med+mjolk"])
            self.assertEqual(q.entries["erud_thid_med+mjolk"].state, "failed", "the answered one is kept over the unseen one")
            self.assertEqual(q.entries["erud_thid_med+mjolk"].items, ["mjolk", "erud_thid_med"], "its items as they were")

    def test_two_answered_entries_keep_the_latest_result_in_either_order(self):
        """Review of #252: both old keys answered, the more recent answer is the learner's progress, whichever was saved first; on the same day,
        the weaker one."""
        older = Entry(items=["mjolk", "erud_thid_med"], prompt="p", answer="A.", source_lesson=1, state="ok", reviews=2, streak=2,
                      last_reviewed="2026-09-20", due="2026-10-01")
        later = Entry(items=PAIR, prompt="p", answer="A.", source_lesson=2, state="failed", reviews=1, last_reviewed="2026-09-25", due=D.isoformat())
        # the same day: which came last is unknown (a date, no time; each entry counts its own reviews), so the weaker result is kept, never a
        # failure lost: an «ok» answered eight times against a «failed» answered once, and an «ok» against a «shaky»
        many_ok = Entry(items=["mjolk", "erud_thid_med"], prompt="p", answer="A.", source_lesson=1, state="ok", reviews=8, streak=8,
                        last_reviewed="2026-09-25", due="2026-10-20")
        once_failed = Entry(items=PAIR, prompt="p", answer="A.", source_lesson=2, state="failed", reviews=1, last_reviewed="2026-09-25", due=D.isoformat())
        same_day = Entry(items=PAIR, prompt="p", answer="A.", source_lesson=2, state="shaky", reviews=1, last_reviewed="2026-09-20", due=D.isoformat())
        for first, second, kept in ((older, later, later), (later, older, later), (many_ok, once_failed, once_failed), (once_failed, many_ok, once_failed),
                                    (older, same_day, same_day), (same_day, older, same_day)):
            with self.subTest(first=first.state, second=second.state), tempfile.TemporaryDirectory() as td:
                path = Path(td) / "queue.json"
                raw = {"format": FORMAT, "items": {"+".join(first.items): first.__dict__, "+".join(second.items): second.__dict__}}
                path.write_text(json.dumps(raw), "utf-8")
                q = ReviewQueue.load(path, D)
                self.assertEqual(list(q.entries), ["erud_thid_med+mjolk"])
                self.assertEqual((q.entries["erud_thid_med+mjolk"].state, q.entries["erud_thid_med+mjolk"].last_reviewed), (kept.state, kept.last_reviewed))


if __name__ == "__main__":
    unittest.main()
