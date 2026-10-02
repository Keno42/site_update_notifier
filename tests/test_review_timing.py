"""振り返りの所要時間の記録 (src/review.py). 実行: python -m unittest discover -s tests -t ."""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from src.review import (
    IDLE_CAP_S,
    REVIEW_LOG,
    log_review,
    read_review_log,
)
from src.weekly import review_time_lines
from tests.test_lesson import session_on

NOW = datetime(2026, 10, 1, 21, 0).astimezone()


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def timed(td, clock):
    s = session_on(td)
    s.clock = clock
    s._shown_at = clock()
    return s


class TimingTests(unittest.TestCase):
    def test_each_answer_is_timed_from_when_it_was_shown(self):
        with tempfile.TemporaryDirectory() as td:
            clock = Clock()
            s = timed(td, clock)
            for sec, result in ((10, "ok"), (30, "failed"), (20, "ok")):
                clock.t += sec
                s.rate(result)
            rec = s.timing_record(NOW, finished=True)
        self.assertEqual(rec["answered"], {"question": 3})
        self.assertEqual(rec["seconds"], {"question": 60})
        self.assertEqual(rec["total_s"], 60)
        self.assertEqual(rec["capped"], 0)
        self.assertTrue(rec["finished"])
        self.assertEqual(rec["planned"], {"question": 3, "scene": 0, "card": 0})

    def test_time_away_from_the_screen_is_capped(self):
        with tempfile.TemporaryDirectory() as td:
            clock = Clock()
            s = timed(td, clock)
            clock.t += 3600  # the learner left
            s.rate("ok")
            clock.t += 15
            s.rate("ok")
            rec = s.timing_record(NOW, finished=False)
        self.assertEqual(rec["total_s"], IDLE_CAP_S + 15)
        self.assertEqual(rec["capped"], 1)
        self.assertFalse(rec["finished"])

    def test_the_log_gets_one_line_per_review_and_skips_empty_ones(self):
        with tempfile.TemporaryDirectory() as td:
            user = Path(td)
            clock = Clock()
            s = timed(td, clock)
            log_review(user, s.timing_record(NOW, finished=False))  # nothing answered
            self.assertFalse((user / REVIEW_LOG).exists())
            clock.t += 20
            s.rate("ok")
            log_review(user, s.timing_record(NOW, finished=False))
            with (user / REVIEW_LOG).open("a") as f:
                f.write("not json\n")
            recs = read_review_log(user)
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["total_s"], 20)
            # only counts and seconds are stored
            self.assertEqual(
                set(json.loads((user / REVIEW_LOG).read_text().splitlines()[0])),
                {
                    "ts",
                    "finished",
                    "planned",
                    "answered",
                    "seconds",
                    "total_s",
                    "capped",
                },
            )
        self.assertEqual(read_review_log(Path("/nonexistent")), [])


class ReportTests(unittest.TestCase):
    def test_the_week_shows_the_typical_review_length(self):
        recs = [
            {
                "finished": True,
                "answered": {"question": 4, "card": 2},
                "seconds": {"question": 120, "card": 60},
                "total_s": 180,
                "capped": 0,
            },
            {
                "finished": False,
                "answered": {"question": 2},
                "seconds": {"question": 100},
                "total_s": 100,
                "capped": 1,
            },
        ]
        (line,) = review_time_lines(recs)
        self.assertIn("2 回、平均 2.3 分（最長 3.0 分）", line)
        self.assertIn("問い 37秒", line)
        self.assertIn("読み 30秒", line)
        self.assertIn("途中で終えた 1 回", line)
        self.assertIn("切った 1 件", line)
        self.assertEqual(review_time_lines([]), [])


if __name__ == "__main__":
    unittest.main()


class BonusSessionTests(unittest.TestCase):
    """#183: the card says it is a bonus before the answer; the weekly line counts asked / said."""

    def test_the_card_says_so_and_the_log_and_weekly_count_bonus_questions(self):
        from datetime import date

        from src.review import BONUS_NOTE, ReviewSession
        from src.review_queue import ReviewQueue

        D = date(2026, 10, 2)
        queue = ReviewQueue()
        queue.add_from_plan(
            {
                "lesson_number": 5,
                "new_items": [],
                "review": [
                    {
                        "items": ["a", "b"],
                        "prompt": "cue",
                        "answer": "Það.",
                        "bonus": True,
                    },
                    {"items": ["c"], "prompt": "cue2", "answer": "Já.", "bonus": True},
                ],
            },
            D,
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "q.json"
            keys = queue.select(D.replace(day=3))
            s = ReviewSession(queue, keys, path, D.replace(day=3))
            self.assertIn(BONUS_NOTE, s.render())
            self.assertNotIn("Það.", s.render())
            s.rate("ok")
            s.rate("failed")
            self.assertEqual(queue.entries, {}, "asked once")
            self.assertEqual(
                queue.reports(), [(5, {"failed": [], "shaky": [], "ok": ["a", "b"]})]
            )
            self.assertEqual(
                s.failed_ids(), [], "a miss on a bonus question changes nothing"
            )
            record = s.timing_record(NOW, True)
            self.assertEqual(record["bonus"], {"asked": 2, "said": 1})
        self.assertTrue(
            any("1 問言えた" in line for line in review_time_lines([record]))
        )
