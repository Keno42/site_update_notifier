"""src/review_queue.py のテスト (issue #31).

実行: python -m unittest discover -s tests -t .
"""

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.review_queue import (
    FORMAT,
    PROMOTE_AFTER_DAYS,
    Entry,
    ReviewQueue,
    next_interval,
)

D = date(2026, 9, 26)


def entry(state="unseen", due=D, new=False, last=None, items=None):
    return Entry(
        items=items or ["x"],
        prompt="p",
        answer="a",
        source_lesson=1,
        state=state,
        new=new,
        last_reviewed=last,
        due=due.isoformat(),
    )


def plan(n, new, review):
    return {
        "lesson_number": n,
        "new_items": [{"id": i} for i in new],
        "review": [
            {"items": items, "prompt": f"say {'+'.join(items)}", "answer": "A."}
            for items in review
        ],
    }


class PolicyTests(unittest.TestCase):
    def test_intervals(self):
        self.assertEqual(
            [next_interval("ok", n) for n in range(1, 7)], [1, 3, 7, 14, 30, 30]
        )
        self.assertEqual([next_interval("shaky", n) for n in range(1, 5)], [1, 3, 7, 7])
        self.assertEqual(next_interval("failed", 3), 1)

    def test_record_updates_only_that_entry(self):
        q = ReviewQueue({"a": entry(), "b": entry()})
        q.record("a", "ok", D)
        q.record("a", "ok", D + timedelta(days=1))
        a, b = q.entries["a"], q.entries["b"]
        self.assertEqual((a.state, a.streak, a.reviews), ("ok", 2, 2))
        self.assertEqual(
            a.due, (D + timedelta(days=4)).isoformat(), "second ok: 3 days"
        )
        self.assertEqual(a.last_reviewed, (D + timedelta(days=1)).isoformat())
        self.assertEqual((b.state, b.reviews, b.due), ("unseen", 0, D.isoformat()))
        q.record("a", "shaky", D + timedelta(days=4))
        self.assertEqual(
            (a.state, a.streak), ("shaky", 1), "a different result restarts"
        )
        self.assertEqual(a.due, (D + timedelta(days=5)).isoformat())

    def test_shaky_comes_back_sooner_than_ok(self):
        q = ReviewQueue({"s": entry(), "o": entry()})
        for _ in range(4):
            q.record("s", "shaky", D)
            q.record("o", "ok", D)
            self.assertLessEqual(q.entries["s"].due, q.entries["o"].due)
        self.assertLess(q.entries["s"].due, q.entries["o"].due, "7 days vs 14 days")


class SelectionTests(unittest.TestCase):
    def test_priority_order(self):
        q = ReviewQueue(
            {
                "later": entry(due=D + timedelta(days=2)),
                "ok": entry("ok"),
                "old_unseen": entry(),
                "shaky": entry("shaky"),
                "new": entry(new=True),
                "failed": entry("failed"),
            }
        )
        self.assertEqual(
            q.select(D, limit=10),
            ["failed", "new", "shaky", "old_unseen", "ok", "later"],
        )
        self.assertEqual(
            q.select(D, limit=0),
            ["failed", "new", "shaky", "old_unseen", "ok"],
            "without a limit only what is due",
        )

    def test_ties_earliest_due_then_oldest_review(self):
        q = ReviewQueue(
            {
                "b": entry("ok", due=D, last="2026-09-20"),
                "c": entry("ok", due=D - timedelta(days=1), last="2026-09-25"),
                "a": entry("ok", due=D, last="2026-09-10"),
            }
        )
        self.assertEqual(q.select(D), ["c", "a", "b"])

    def test_limit_and_carry_over(self):
        q = ReviewQueue({f"q{i}": entry(items=[f"i{i}"]) for i in range(5)})
        first = q.select(D, limit=2)
        self.assertEqual(len(first), 2)
        for k in first:
            q.record(k, "ok", D)
        left = [k for k, e in q.entries.items() if e.state == "unseen"]
        self.assertEqual(
            len(left), 3, "not asked: still unreviewed, not passed or removed"
        )
        second = q.select(D + timedelta(days=1), limit=2)
        self.assertTrue(
            set(second) <= set(left), "tomorrow asks the carried-over ones first"
        )

    def test_long_waiting_items_are_not_starved_by_new_ones(self):
        q = ReviewQueue({"old": entry("ok", due=D)})
        asked_on = None
        for day in range(PROMOTE_AFTER_DAYS + 2):
            today = D + timedelta(days=day)
            q.add_from_plan(
                plan(
                    day + 2,
                    [f"n{day}_{i}" for i in range(4)],
                    [[f"n{day}_{i}"] for i in range(4)],
                ),
                today - timedelta(days=1),
            )
            for k in q.select(today, limit=3):
                if k == "old":
                    asked_on = day
                q.record(k, "ok", today)
            if asked_on is not None:
                break
        self.assertIsNotNone(
            asked_on, "an overdue item reaches the front despite daily new items"
        )
        self.assertLessEqual(asked_on, PROMOTE_AFTER_DAYS)


class PlanTests(unittest.TestCase):
    def test_new_items_join_due_tomorrow_and_nothing_is_replaced(self):
        q = ReviewQueue()
        q.add_from_plan(plan(1, ["a"], [["a"], ["b"], ["c", "d"]]), D)
        self.assertEqual(
            set(q.entries), {"a", "b", "c+d"}, "a shared sentence stays one entry"
        )
        self.assertTrue(q.entries["a"].new)
        self.assertFalse(q.entries["b"].new)
        self.assertEqual(q.entries["a"].due, (D + timedelta(days=1)).isoformat())
        q.record("a", "failed", D + timedelta(days=1))
        added = q.add_from_plan(
            plan(2, [], [["a"], ["b"], ["d"], ["e"]]), D + timedelta(days=1)
        )
        self.assertEqual(added, 1, "only e: a and b are queued, d is covered by c+d")
        self.assertEqual(
            q.entries["a"].state, "failed", "an existing entry keeps its state"
        )
        self.assertEqual(q.entries["a"].prompt, "say a")


class StorageTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pending_review.json"
            q = ReviewQueue({"a": entry("shaky", last="2026-09-25")})
            q.save(path)
            raw = json.loads(path.read_text("utf-8"))
            self.assertEqual(raw["format"], FORMAT)
            self.assertEqual(ReviewQueue.load(path, D).entries, q.entries)
            self.assertEqual(
                [p.name for p in Path(td).iterdir()], ["pending_review.json"]
            )

    def test_legacy_file_is_migrated_and_backed_up(self):
        legacy = {
            "lesson": 7,
            "questions": [
                {
                    "items": ["fyrirgefdu"],
                    "prompt": "Say: Excuse me.",
                    "answer": "Fyrirgefðu.",
                },
                {
                    "items": ["eg_vil", "fara_heim"],
                    "prompt": "帰りたい",
                    "answer": "Ég vil fara heim.",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pending_review.json"
            path.write_text(json.dumps(legacy, ensure_ascii=False), "utf-8")
            q = ReviewQueue.load(path, D)
            self.assertEqual(set(q.entries), {"fyrirgefdu", "eg_vil+fara_heim"})
            for e in q.entries.values():
                self.assertEqual(
                    (e.state, e.reviews, e.last_reviewed, e.due, e.source_lesson),
                    ("unseen", 0, None, D.isoformat(), 7),
                )
            bak = Path(td) / "pending_review.json.v1.bak"
            self.assertEqual(json.loads(bak.read_text("utf-8")), legacy)
            self.assertEqual(json.loads(path.read_text("utf-8"))["format"], FORMAT)
            self.assertEqual(
                ReviewQueue.load(path, D + timedelta(days=3)).entries,
                q.entries,
                "migrated once; later loads read the new format",
            )

    def test_missing_or_broken_file_is_an_empty_queue(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pending_review.json"
            self.assertEqual(ReviewQueue.load(path, D).entries, {})
            path.write_text("{not json", "utf-8")
            self.assertEqual(ReviewQueue.load(path, D).entries, {})
            broken = Path(td) / "pending_review.json.broken"
            self.assertEqual(
                broken.read_text("utf-8"), "{not json", "set aside, not overwritten"
            )


if __name__ == "__main__":
    unittest.main()
