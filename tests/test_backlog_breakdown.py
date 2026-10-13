"""#123: «確認待ち N件» with its breakdown."""
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.feedback import Ledger
from src.review import review_note
from src.review_queue import Entry, ReviewQueue

D = date(2026, 10, 1)
PAST = (D - timedelta(days=1)).isoformat()
FUTURE = (D + timedelta(days=30)).isoformat()


def queue():
    q = ReviewQueue()

    def add(key, **kw):
        base = dict(items=[key], prompt=f"{key}?", answer=key, source_lesson=1, due=PAST)
        base.update(kw)
        q.entries[key] = Entry(**base)

    add("n1", state="unseen", new=True)
    add("u1", state="unseen")
    add("u2", state="unseen")
    add("once", state="ok", reviews=1)
    add("twice", state="ok", reviews=2)
    add("thrice", state="ok", reviews=5)
    add("shaky", state="shaky", reviews=1)
    add("failed", state="failed", reviews=1)
    add("open", state="unseen", open=True)
    add("later", state="ok", reviews=3, due=FUTURE)  # not due: not in the breakdown, but in the total
    return q


class BacklogTests(unittest.TestCase):
    def test_the_parts_add_up_to_the_due_count(self):
        q = queue()
        parts = q.backlog_parts(D)
        self.assertEqual(sum(parts.values()), q.due_count(D))
        self.assertEqual(parts, {"未出題": 2, "1回": 1, "言えた×2+": 2, "迷った/言えなかった": 2, "未解決": 1, "新出": 1})

    def test_an_empty_part_is_left_out(self):
        q = ReviewQueue()
        q.entries["a"] = Entry(["a"], "a?", "a", 1, state="ok", reviews=3, due=PAST)
        self.assertEqual(q.backlog_parts(D), {"言えた×2+": 1})

    def test_the_note_names_the_parts(self):
        note = review_note(queue(), D, 14)
        self.assertIn("確認待ち 9件: 未出題 2・1回 1・言えた×2+ 2・迷った/言えなかった 2・未解決 1・新出 1", note)

    def test_the_export_carries_counts_only_and_the_total(self):
        q = queue()
        summary = q.backlog_summary(D)
        self.assertEqual((summary["due"], summary["total"]), (9, 10))
        self.assertNotIn("n1", json.dumps(summary, ensure_ascii=False), "counts, no items")
        with tempfile.TemporaryDirectory() as td:
            ledger = Ledger(Path(td))
            d = ledger.manifests / "lesson-001"
            d.mkdir(parents=True)
            (d / "manifest.json").write_text(json.dumps({"lesson": 1}), "utf-8")
            self.assertTrue(ledger.annotate_manifest("lesson-001", {"review_backlog": summary}))
            self.assertEqual(json.loads((d / "manifest.json").read_text("utf-8"))["review_backlog"], summary)
            self.assertFalse(ledger.annotate_manifest("lesson-404", {"x": 1}), "a missing record is not an error")


if __name__ == "__main__":
    unittest.main()
