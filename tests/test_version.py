"""src/version.py のテスト. 実行: python -m unittest discover -s tests -t ."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.version import REPO_DIR, Commit, VersionInfo, head_commit, uptime

JST = timezone(timedelta(hours=9))


class VersionTests(unittest.TestCase):
    def test_head_commit_reads_this_repository(self):
        c = head_commit(REPO_DIR)
        self.assertIsNotNone(c)
        assert c is not None
        self.assertRegex(c.short, r"^[0-9a-f]{7,}$")
        self.assertRegex(c.date, r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
        self.assertTrue(c.subject)

    def test_head_commit_outside_a_repository_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(head_commit(Path(d) / "missing"))

    def test_uptime(self):
        self.assertEqual(uptime(59), "0分")
        self.assertEqual(uptime(3 * 3600 + 5 * 60), "3時間5分")
        self.assertEqual(uptime(2 * 86400 + 4 * 3600 + 59), "2日4時間")

    def test_text_shows_start_time_uptime_and_both_commits(self):
        started = datetime(2026, 9, 28, 9, 0, tzinfo=JST)
        info = VersionInfo(
            started=started,
            bot=Commit("abc1234", "2026-09-28 08:55", "Merge pull request #45"),
            lla=Commit("b429c96", "2026-09-28 08:30", "x" * 100),
        )
        text = info.text(now=started + timedelta(hours=2, minutes=15))
        lines = text.splitlines()
        self.assertEqual(lines[0], "起動: 2026-09-28 09:00:00 +0900（稼働 2時間15分）")
        self.assertEqual(
            lines[1], "**bot** `abc1234`（2026-09-28 08:55）Merge pull request #45"
        )
        self.assertTrue(lines[2].startswith("**language-learning-audio** `b429c96`"))
        self.assertTrue(lines[2].endswith("…"))

    def test_text_says_unknown_when_git_is_unavailable(self):
        started = datetime(2026, 9, 28, 9, 0, tzinfo=JST)
        text = VersionInfo(started=started, bot=None, lla=None).text(now=started)
        self.assertIn("**bot** 不明", text)
        self.assertIn("**language-learning-audio** 不明", text)


if __name__ == "__main__":
    unittest.main()
