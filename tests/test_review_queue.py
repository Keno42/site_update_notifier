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
            ["new", "failed", "shaky", "old_unseen", "ok", "later"],
        )
        self.assertEqual(
            q.select(D, limit=0),
            ["new", "failed", "shaky", "old_unseen", "ok"],
            "without a limit only what is due",
        )

    def test_the_last_lessons_new_items_come_first(self):
        """Issue #119: an unanswered item counts as recalled on the audio side, so the last
        lesson's new items are asked first — before failures, before long-waiting
        questions, and before an older lesson's still unanswered new items."""
        waiting = {f"old{i}": entry("ok", due=D - timedelta(days=30)) for i in range(5)}
        q = ReviewQueue(waiting | {"f": entry("failed")})
        older = entry(new=True, due=D - timedelta(days=5))
        older.source_lesson = 6
        q.entries["older_new"] = older
        q.add_from_plan(plan(7, ["a", "b"], [["a"], ["b"]]), D)
        self.assertEqual(
            q.select(D, limit=3), ["a", "b", "older_new"], "the same day too"
        )
        q.record("a", "ok", D)
        self.assertNotIn("a", q.select(D, limit=3), "once answered, no longer first")

    def test_every_new_item_of_the_last_lesson_is_asked_past_the_limit(self):
        """#38 review: /lesson generates only after the last lesson's new items are all
        answered, so a session holds all of them even beyond the limit."""
        q = ReviewQueue({"f": entry("failed")})
        q.add_from_plan(plan(7, list("abcde"), [[i] for i in "abcde"]), D)
        self.assertEqual(sorted(q.must_answer()), list("abcde"))
        self.assertEqual(
            sorted(q.select(D, limit=3)), list("abcde"), "all five, no room"
        )
        self.assertEqual(q.select(D, limit=6)[-1], "f", "the rest fill what is left")
        for k in "abcde":
            q.record(k, "ok", D)
        self.assertEqual(q.must_answer(), [])

    def test_an_older_lessons_unanswered_new_items_leave_the_queue(self):
        """#38 review: a new item left unanswered when the next lesson was generated (a
        /lesson-auto in between) was presumed recalled by the audio lesson; it leaves the
        queue instead of waiting forever behind newer new items. Answered and non-new
        questions stay, and the item can come back as an ordinary question later."""
        q = ReviewQueue()
        q.add_from_plan(plan(6, ["old", "done"], [["old"], ["done"], ["rev"]]), D)
        q.record("done", "shaky", D)
        q.add_from_plan(plan(7, ["new"], [["new"]]), D + timedelta(days=1))
        self.assertEqual(q.drop_stale_new(), ["old"])
        self.assertEqual(sorted(q.entries), ["done", "new", "rev"])
        self.assertEqual(q.drop_stale_new(), [], "the last lesson's are kept")
        q.add_from_plan(plan(8, [], [["old"]]), D + timedelta(days=2))
        self.assertFalse(q.entries["old"].new, "back as an ordinary question")

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
        """The last lesson's new items fill a session first (#119); with room left over, an
        overdue question still reaches the front within PROMOTE_AFTER_DAYS. (A session whose
        limit the new items alone use up asks only them.)"""
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
            for k in q.select(today, limit=5):
                if k == "old":
                    asked_on = day
                q.record(k, "ok", today)
            if asked_on is not None:
                break
        self.assertIsNotNone(
            asked_on, "an overdue item reaches the front despite daily new items"
        )
        self.assertLessEqual(asked_on, PROMOTE_AFTER_DAYS)


class ReportBookkeepingTests(unittest.TestCase):
    """PR #32 review: answers wait in the queue, per source lesson, until the audio
    lesson side (audiolesson report) has them."""

    def queue(self):
        def at(lesson, item):
            e = entry(items=[item])
            e.source_lesson = lesson
            return e

        return ReviewQueue(
            {"a": at(3, "a"), "b": at(7, "b"), "c": at(7, "c"), "d": at(7, "d")}
        )

    def test_answers_are_grouped_by_source_lesson(self):
        q = self.queue()
        q.record("a", "failed", D)
        q.record("b", "ok", D)
        q.record("c", "failed", D)
        self.assertEqual(q.reports(), [(3, ["a"]), (7, ["c"])])
        q.mark_reported(3, ["a"])
        self.assertEqual(q.reports(), [(7, ["c"])])

    def test_an_ok_only_lesson_still_needs_a_report(self):
        q = self.queue()
        q.record("b", "ok", D)
        self.assertEqual(q.reports(), [(7, [])], "reported without --failed")
        q.mark_reported(7, [])
        self.assertEqual(q.reports(), [])

    def test_answers_given_while_a_report_is_in_flight_are_kept(self):
        q = self.queue()
        q.record("c", "failed", D)
        sent = q.reports()[0][1]
        q.record("d", "failed", D)  # answered during the CLI call
        q.mark_reported(7, sent)
        self.assertEqual(q.reports(), [(7, ["d"])])

    def test_pending_reports_survive_a_restart(self):
        q = self.queue()
        q.record("a", "failed", D)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pending_review.json"
            q.save(path)
            self.assertEqual(ReviewQueue.load(path, D).reports(), [(3, ["a"])])


class PlanTests(unittest.TestCase):
    def test_new_items_are_due_at_once_and_nothing_is_replaced(self):
        q = ReviewQueue()
        q.add_from_plan(plan(1, ["a"], [["a"], ["b"], ["c", "d"]]), D)
        self.assertEqual(
            set(q.entries), {"a", "b", "c+d"}, "a shared sentence stays one entry"
        )
        self.assertTrue(q.entries["a"].new)
        self.assertFalse(q.entries["b"].new)
        self.assertEqual(q.entries["a"].due, D.isoformat(), "asked at the next /lesson")
        self.assertEqual(q.entries["b"].due, (D + timedelta(days=1)).isoformat())
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
