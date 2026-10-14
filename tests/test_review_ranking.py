"""#220: the review's retention seats go to the older items a 言えた would save most lesson time for."""
import unittest
from datetime import date, timedelta

from src.review_queue import Entry, ReviewQueue

D = date(2026, 10, 14)


def queue(n=12):
    q = ReviewQueue()
    for k in range(n):  # older and older confirmations: e0 is the most recently confirmed
        q.entries[f"e{k}"] = Entry(items=[f"i{k}"], prompt="p", answer="a", source_lesson=2, state="ok", reviews=2, streak=2,
                                   last_reviewed=(D - timedelta(days=4 + k)).isoformat(), due=(D - timedelta(days=1)).isoformat())
    return q


class RankingTests(unittest.TestCase):
    def test_without_savings_the_retention_seats_go_to_the_oldest(self):
        keys = queue().select(D, 9)
        self.assertEqual({"e11", "e10", "e9"}, {k for k in keys if k in ("e11", "e10", "e9")}, "the three seats of a third: the oldest")

    def test_with_savings_a_third_of_the_seats_go_to_the_items_that_pay_and_the_rest_stays_oldest_first(self):
        q = queue(30)
        savings = {"i2": 40.0, "i5": 30.0, "i1": 20.0, "i7": 5.0}  # i7 is under the threshold: its check costs about what it saves
        keys = q.select(D, 18, savings=savings)  # six retention seats
        self.assertTrue({"e2", "e5"} <= set(keys), "the two that save most: a third of six seats")
        self.assertTrue({"e29", "e28", "e27", "e26"} <= set(keys), "and four seats stay with the oldest, for an unbiased sample")
        self.assertEqual(len(keys), 18)
        self.assertNotIn("e7", keys, "an item under the threshold is not ranked")
        self.assertNotIn("e1", keys, "a third of the seats: the third-best saving waits")

    def test_savings_that_name_no_due_item_change_nothing(self):
        q = queue()
        self.assertEqual(q.select(D, 9, savings={"nothing": 99.0}), q.select(D, 9))
        self.assertEqual(q.select(D, 9, savings={}), q.select(D, 9))

    def test_a_question_holding_several_items_counts_by_the_most_it_saves(self):
        q = queue()
        q.entries["e3"].items = ["i3", "i3b"]
        keys = q.select(D, 9, savings={"i3b": 50.0, "i4": 49.0})
        self.assertIn("e3", keys)

    def test_open_new_and_failed_entries_are_not_retention_seats(self):
        q = queue(4)
        q.entries["e0"].open = True
        q.entries["e1"].state = "failed"
        keys = q.select(D, 9, savings={"i0": 99.0, "i1": 98.0, "i2": 40.0})
        self.assertIn("e2", keys)


class DryRunTests(unittest.TestCase):
    """The bot's dry run before the review: `generate --dry-run --no-audio`, whose plan.json carries ``review_savings``."""

    def run_dry(self, fake):
        import asyncio
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock

        from src.lesson import LessonConfig, Lessons
        from tests.test_lesson import FakeChannel

        with tempfile.TemporaryDirectory() as td:
            cfg = LessonConfig(root=Path(td), users={1: "yuki"})
            cfg.user_dir("yuki").mkdir(parents=True)
            cfg.learner_path("yuki").write_text(json.dumps({"format": "x"}), "utf-8")
            calls = []

            async def cli(cfg_, args, on_progress=None, stdin=None, env=None):
                calls.append(args)
                return fake(args)

            with mock.patch("src.lesson.run_cli", cli):
                out = asyncio.run(Lessons(cfg).review_savings(FakeChannel(), "yuki", 30))
        return out, calls

    def test_it_reads_the_savings_of_the_dry_runs_plan_and_never_touches_the_real_work_dir(self):
        import json
        from pathlib import Path

        def fake(args):
            out = Path(args[args.index("--out") + 1])
            (out / "lesson-005.plan.json").write_text(json.dumps({"review_savings": {"a": 21.5, "b": 40}}), "utf-8")
            return 0, "", ""

        got, calls = self.run_dry(fake)
        self.assertEqual(got, {"a": 21.5, "b": 40.0})
        args = calls[0]
        self.assertIn("--dry-run", args)
        self.assertIn("--no-audio", args)
        self.assertNotIn("work", Path(args[args.index("--out") + 1]).name, "a scratch directory, not the lesson's work directory")

    def test_an_older_generator_or_a_failure_gives_none_and_the_selection_goes_on_as_before(self):
        import json
        from pathlib import Path

        def old(args):  # a plan.json without the field
            (Path(args[args.index("--out") + 1]) / "lesson-005.plan.json").write_text(json.dumps({"review": []}), "utf-8")
            return 0, "", ""

        self.assertIsNone(self.run_dry(old)[0])
        self.assertIsNone(self.run_dry(lambda args: (2, "", "error: boom"))[0])


if __name__ == "__main__":
    unittest.main()
