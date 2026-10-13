"""LLA #298 review (bot #118): a generator that does not know --gaps still gets the outcomes, and generation goes on."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.lesson import LessonConfig, Lessons
from datetime import date

from src.review_queue import Entry, ReviewQueue


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, text, **kw):
        self.sent.append(text)


class GapsFallbackTests(unittest.TestCase):
    def queue(self):
        q = ReviewQueue()
        q.pending_reports[4] = {"failed": [], "shaky": [], "ok": ["a", "b"]}
        q.pending_gaps[4] = {"a": 14, "b": 9}
        return q

    def run_flush(self, accepts_gaps):
        calls = []

        async def cli(cfg, args, on_progress=None):
            calls.append(args)
            if "--gaps" in args and not accepts_gaps:
                return 2, "", "audiolesson: error: unrecognized arguments: --gaps a=14,b=9"
            return 0, "", ""

        with tempfile.TemporaryDirectory() as td:
            cfg = LessonConfig(root=Path(td), users={1: "yuki"})
            cfg.user_dir("yuki").mkdir(parents=True)
            queue, channel = self.queue(), FakeChannel()
            with mock.patch("src.lesson.run_cli", cli):
                ok = asyncio.run(Lessons(cfg).flush_reports(channel, "yuki", queue, cfg.pending_path("yuki")))
        return ok, calls, queue, channel

    def test_an_older_generator_gets_the_outcomes_without_gaps_and_the_gaps_are_kept(self):
        ok, calls, queue, channel = self.run_flush(accepts_gaps=False)
        self.assertTrue(ok, "the flush succeeds, so lessons are generated")
        self.assertEqual(len(calls), 2)
        self.assertIn("--gaps", calls[0])
        self.assertNotIn("--gaps", calls[1])
        self.assertIn("a,b", calls[1][calls[1].index("--recalled") + 1])
        self.assertEqual(queue.pending_gaps, {}, "a gap belongs to the answer it was measured on: dropped with it")
        self.assertEqual(queue.reports(), [])
        self.assertEqual(channel.sent, [])

    def test_the_next_answer_with_a_short_gap_sends_no_stale_long_gap(self):
        from datetime import date, timedelta
        ok, calls, queue, _ = self.run_flush(accepts_gaps=False)
        self.assertTrue(ok)
        day = date(2026, 10, 10)
        queue.entries["a"] = Entry(["a"], "a?", "a", 4, state="ok", reviews=2, due=day.isoformat(), last_reviewed=(day - timedelta(days=1)).isoformat())
        queue.record("a", "ok", day)
        self.assertNotIn("a", queue.pending_gaps.get(4, {}), "a one-day gap is no gap, and no old one is left to be sent")

    def test_a_queue_file_with_gaps_for_already_reported_items_loads_without_them(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pending.json"
            q = self.queue()
            q.pending_gaps[4] = {"a": 14, "b": 9, "gone": 20}  # «gone» has no outcome waiting
            q.pending_gaps[3] = {"old": 11}  # lesson 3 has nothing waiting at all
            q.save(path)
            loaded = ReviewQueue.load(path, date(2026, 10, 10))
        self.assertEqual(loaded.pending_gaps, {4: {"a": 14, "b": 9}})

    def test_a_current_generator_gets_the_gaps_once_and_they_are_cleared(self):
        ok, calls, queue, _ = self.run_flush(accepts_gaps=True)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(queue.pending_gaps, {})


if __name__ == "__main__":
    unittest.main()
