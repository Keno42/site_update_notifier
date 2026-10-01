"""src/levers.py と src/topic.py のテスト. 実行: python -m unittest discover -s tests -t ."""

import unittest
from types import SimpleNamespace

from src.levers import apply, from_topic, parse
from src.topic import TopicError, section

TOPIC = """アイスランド語 🇮🇸
[trip]
season = "winter-holidays"
[levers]
max_same_situation = 1
late_unhinted_recall = true
pause_multiplier = 1.2
"""


class ParseTests(unittest.TestCase):
    def test_a_levers_section_becomes_generate_arguments(self):
        self.assertEqual(section(TOPIC, "trip"), {"season": "winter-holidays"})
        self.assertEqual(
            parse(section(TOPIC, "levers")),
            [
                "--max-same-situation",
                "1",
                "--late-unhinted-recall",
                "--pause-multiplier",
                "1.2",
            ],
        )

    def test_the_one_line_form_and_turning_levers_off(self):
        topic = "levers = { max_same_situation = 0, late_unhinted_recall = false }"
        self.assertEqual(parse(section(topic, "levers")), [])
        self.assertIsNone(section("雑談チャンネル", "levers"))

    def test_bad_values_are_named_without_echoing_them(self):
        for raw in (
            {"max_same_situation": -1},
            {"max_same_situation": "one"},
            {"late_unhinted_recall": "yes"},
            {"pause_multiplier": 9},
            {"new_items": 12},
        ):
            with self.subTest(raw=raw), self.assertRaises(TopicError):
                parse(raw)

    def test_a_broken_section_warns_and_keeps_the_config(self):
        channel = SimpleNamespace(topic='[levers]\nlate_unhinted_recall = "yes"\n')
        args, warning = from_topic(channel)
        self.assertIsNone(args)
        self.assertIn("レバーの設定を読めませんでした", warning)
        self.assertEqual(from_topic(SimpleNamespace(topic="雑談")), (None, ""))


class ApplyTests(unittest.TestCase):
    EXTRA = [
        "--max-same-situation",
        "2",
        "--late-unhinted-recall",
        "--provider",
        "stub",
    ]

    def test_the_topic_replaces_the_configs_levers_only(self):
        self.assertEqual(
            apply(self.EXTRA, ["--max-same-situation", "1"]),
            ["--provider", "stub", "--max-same-situation", "1"],
        )
        self.assertEqual(apply(self.EXTRA, []), ["--provider", "stub"], "all off")
        self.assertEqual(apply(["--pause-multiplier=1.5", "--auto"], []), ["--auto"])

    def test_without_a_levers_section_the_config_stays(self):
        self.assertEqual(apply(self.EXTRA, None), self.EXTRA)


if __name__ == "__main__":
    unittest.main()
