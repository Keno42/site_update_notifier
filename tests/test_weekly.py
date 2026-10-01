"""src/weekly.py のテスト. 実行: python -m unittest discover -s tests -t ."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.feedback import FEEDBACK_FILE
from src.weekly import build, item_outcome, lever_flags, word_count

TODAY = date(2026, 10, 1)


def item(lesson_outcomes: dict[int, str | bool]) -> dict:
    """lesson → 結果 ("recalled" など) か、結果なしの presumed success (True) / 失敗 (False)."""
    history = []
    for n, o in lesson_outcomes.items():
        h = {
            "lesson": n,
            "stages": ["meaning"],
            "ok": o is not False and o != "not_recalled",
        }
        if isinstance(o, str):
            h["outcome"] = o
        history.append(h)
    return {"history": history}


def write(user_dir: Path, learner: dict, plans: dict[str, dict] | None = None) -> None:
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "learner.json").write_text(json.dumps(learner), "utf-8")
    for name, (manifest, plan) in (plans or {}).items():
        d = user_dir / "lesson_manifests" / name
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps(manifest), "utf-8")
        (d / f"{name}.plan.json").write_text(json.dumps(plan), "utf-8")


LEARNER = {
    "lessons": [
        {
            "number": 9,
            "date": "2026-09-20",
            "new_items": ["old"],
            "pace": 4,
            "duration_s": 1700,
        },
        {
            "number": 10,
            "date": "2026-09-29",
            "new_items": ["a", "b", "c"],
            "pace": 5,
            "duration_s": 1750,
        },
        {
            "number": 11,
            "date": "2026-09-30",
            "new_items": ["d", "e", "f"],
            "pace": 5,
            "duration_s": 1500,
        },
    ],
    "items": {
        "old": item({9: "not_recalled"}),
        "a": item({10: "recalled"}),
        "b": item({10: "not_recalled"}),
        "c": item({10: True}),
        "d": item({11: "recalled"}),
        "e": item({11: "hesitated"}),
        "f": item({11: "recalled"}),
    },
}
PLANS = {
    "lesson-011": (
        {
            "lesson": 11,
            "created_at": "2026-09-30T20:00:00+09:00",
            "revisions": {"lla": "c857a6f0000"},
            "generate_args": [
                "generate",
                "--learner",
                "/x/learner.json",
                "--late-unhinted-recall",
            ],
        },
        {
            "new_items": [
                {"id": "a", "target": "Takk."},
                {"id": "b", "target": "Eigðu góðan dag."},
                {"id": "e", "target": "Hvað kostar þetta?"},
                {"id": "d", "target": "Já."},
            ]
        },
    ),
}


class HelpersTests(unittest.TestCase):
    def test_outcome_falls_back_to_the_presumed_schedule(self):
        it = item({10: True, 11: False, 12: "hesitated"})
        self.assertEqual(item_outcome(it, 10), "unreported")
        self.assertEqual(item_outcome(it, 11), "not_recalled")
        self.assertEqual(item_outcome(it, 12), "hesitated")
        self.assertEqual(item_outcome(it, 99), "unreported")

    def test_words_and_lever_flags(self):
        self.assertEqual(word_count("Takk fyrir {thing}."), 3)
        self.assertEqual(word_count("Já."), 1)
        self.assertEqual(
            lever_flags(
                ["generate", "--learner", "/p", "--pause-multiplier", "1.2", "--auto"]
            ),
            "--pause-multiplier 1.2 --auto",
        )
        self.assertEqual(lever_flags(["generate", "--learner", "/p"]), "なし")


class BuildTests(unittest.TestCase):
    def test_a_week_of_signals(self):
        with tempfile.TemporaryDirectory() as td:
            user = Path(td) / "yuki"
            write(user, LEARNER, PLANS)
            (user / FEEDBACK_FILE).write_text(
                json.dumps(
                    {
                        "ts": "2026-09-30T21:00:00+09:00",
                        "lesson": 11,
                        "load": "right",
                        "friction": ["repetitive"],
                        "note": "kaupi meði は聞こえなかった",
                    },
                    ensure_ascii=False,
                )
                + "\n"
                + json.dumps(
                    {"ts": "2026-08-01T21:00:00+09:00", "lesson": 1, "load": "heavy"}
                )
                + "\n",
                "utf-8",
            )
            text = build(user, TODAY, 7)
        self.assertIn("レッスン 10〜11（2回）", text, "lesson 9 is older than a week")
        self.assertIn("10: 新出 3（ペース 5）", text)
        self.assertIn("6 個のうち 言えた 3・迷った 1・言えなかった 1・未確認 1", text)
        self.assertIn("レッスン 10: Eigðu góðan dag.", text)
        self.assertIn("レッスン 11: Hvað kostar þetta?", text)
        self.assertIn("フィードバック 1 件", text)
        self.assertIn("ちょうどいい 1", text)
        self.assertIn("繰り返しが多い 1", text)
        self.assertIn("kaupi meði", text)
        self.assertIn("レッスン 11〜: LLA `c857a6f`、引数 --late-unhinted-recall", text)
        self.assertNotIn("/x/learner.json", text, "paths stay out")
        self.assertNotIn("heavy", text)

    def test_long_phrases_against_short_ones(self):
        with tempfile.TemporaryDirectory() as td:
            user = Path(td) / "yuki"
            learner = json.loads(json.dumps(LEARNER))
            learner["items"]["d"] = item({11: "not_recalled"})
            write(user, learner, PLANS)
            text = build(user, TODAY, 7)
        # b (3 words) failed; a, d, e (1-3 words): d (Já.) failed, e (3 words) hesitated, not failed
        self.assertIn("1〜2語 1/2、3語以上 1/2", text)

    def test_an_empty_week_and_a_missing_learner(self):
        with tempfile.TemporaryDirectory() as td:
            user = Path(td) / "yuki"
            self.assertIn("まだレッスンの記録がない", build(user, TODAY))
            write(user, LEARNER)
            text = build(user, date(2026, 12, 1), 7)
        self.assertIn("この期間のレッスンはありません", text)

    def test_the_message_fits_in_discord(self):
        with tempfile.TemporaryDirectory() as td:
            user = Path(td) / "yuki"
            learner = {
                "lessons": [
                    {
                        "number": n,
                        "date": "2026-09-30",
                        "new_items": [],
                        "pace": 5,
                        "duration_s": 1700,
                    }
                    for n in range(1, 400)
                ],
                "items": {},
            }
            write(user, learner)
            self.assertLessEqual(len(build(user, TODAY, 7)), 1900)


if __name__ == "__main__":
    unittest.main()
