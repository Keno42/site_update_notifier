"""src/trip.py のテスト. 実行: python -m unittest discover -s tests -t ."""

import hashlib
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from src.trip import (
    TOPIC,
    TripError,
    channel_topic,
    dump,
    parse_topic,
    recorded_args,
    resolve,
)

SECTION = """アイスランド旅行 🇮🇸
[trip]
departure = 2030-01-31
boost = ["A6", "B2"]
places = ["Testvík", "Prófnes"]
season = "winter-holidays"

ほかのメモ
"""
EXPECTED = {
    "departure": date(2030, 1, 31),
    "boost": ["A6", "B2"],
    "places": ["Testvík", "Prófnes"],
    "season": "winter-holidays",
}


class ParseTopicTests(unittest.TestCase):
    def test_a_trip_section(self):
        self.assertEqual(parse_topic(SECTION), EXPECTED)

    def test_a_one_line_inline_table(self):
        topic = (
            "旅行の練習 | trip ではない行\n"
            'trip = { departure = 2030-01-31, boost = ["A6", "B2"], '
            'places = ["Testvík", "Prófnes"], season = "winter-holidays" }'
        )
        self.assertEqual(parse_topic(topic), EXPECTED)

    def test_a_code_fence_and_the_next_header_end_the_section(self):
        topic = '```toml\n[trip]\nseason = "winter-holidays"\n[other]\nx = 1\n```'
        self.assertEqual(parse_topic(topic), {"season": "winter-holidays"})

    def test_no_trip_means_none(self):
        self.assertIsNone(parse_topic(""))
        self.assertIsNone(parse_topic("アイスランド語の練習チャンネル"))
        self.assertEqual(parse_topic("[trip]\n"), {}, "empty: the default ordering")

    def test_errors_name_keys_and_positions_never_values(self):
        cases = [
            '[trip]\nplaces = ["Testvík"\n',
            '[trip]\nhotel = "Testvík"\n',
            '[trip]\nplaces = "Testvík"\n',
            '[trip]\ndeparture = "Testvík"\n',
            '[trip]\nseason = ["Testvík"]\n',
            'trip = { hotel = "Testvík" }',
        ]
        for topic in cases:
            with self.subTest(topic=topic):
                with self.assertRaises(TripError) as cm:
                    parse_topic(topic)
                self.assertNotIn("Testv", str(cm.exception))

    def test_dump_is_canonical(self):
        text = dump(EXPECTED)
        self.assertEqual(
            text,
            "departure = 2030-01-31\n"
            'boost = ["A6", "B2"]\n'
            'places = ["Testvík", "Prófnes"]\n'
            'season = "winter-holidays"\n',
        )
        inline = parse_topic(
            'trip = {season="winter-holidays", places=["Testvík","Prófnes"],'
            ' boost=["A6","B2"], departure="2030-01-31"}'
        )
        self.assertEqual(dump(inline), text, "same contents, same file")

    def test_thread_uses_the_parent_topic(self):
        parent = SimpleNamespace(topic=SECTION)
        self.assertEqual(channel_topic(SimpleNamespace(parent=parent)), SECTION)
        self.assertEqual(channel_topic(SimpleNamespace(topic=None)), "")
        self.assertEqual(channel_topic(object()), "")


class ResolveTests(unittest.TestCase):
    def test_the_topic_becomes_a_temporary_file(self):
        with tempfile.TemporaryDirectory() as td:
            channel = SimpleNamespace(topic=SECTION)
            with resolve(Path(td) / "trip.toml", channel) as (source, warning):
                self.assertEqual(warning, "")
                self.assertEqual(source.origin, TOPIC)
                text = source.path.read_bytes()
                self.assertEqual(text, dump(EXPECTED).encode())
                self.assertEqual(source.sha256, hashlib.sha256(text).hexdigest())
                args = ["generate", "--trip", str(source.path)]
                self.assertEqual(
                    recorded_args(args, source), ["generate", "--trip", TOPIC]
                )
            self.assertFalse(source.path.exists(), "removed after use")

    def test_the_learners_own_file_wins(self):
        with tempfile.TemporaryDirectory() as td:
            file = Path(td) / "trip.toml"
            file.write_text('season = "summer"\n', "utf-8")
            channel = SimpleNamespace(topic=SECTION)
            with resolve(file, channel) as (source, _):
                self.assertEqual((source.path, source.origin), (file, "file"))
                args = ["generate", "--trip", str(file)]
                self.assertEqual(recorded_args(args, source), args)

    def test_nothing_or_a_broken_topic(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "trip.toml"
            with resolve(missing, SimpleNamespace(topic="雑談")) as (source, warning):
                self.assertEqual((source, warning), (None, ""))
            broken = SimpleNamespace(topic='[trip]\nhotel = "Testvík"\n')
            with resolve(missing, broken) as (source, warning):
                self.assertIsNone(source)
                self.assertIn("トピックの旅程の設定を読めませんでした", warning)
                self.assertIn("hotel", warning)
                self.assertNotIn("Testv", warning)


if __name__ == "__main__":
    unittest.main()
