"""src/reading.py と、振り返りの読みカードのテスト. 実行: python -m unittest discover -s tests -t ."""

import asyncio
import contextlib
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from src.lesson import LessonConfig, Lessons, ReviewSession, ReviewView
from src.reading import ReadingQueue, parse_deck, profile_voice, render_card
from src.review_queue import ReviewQueue

D = date(2026, 9, 26)
DECK = [
    {
        "id": "opid",
        "stage": "signs",
        "text": "Opið",
        "meaning": "open",
        "meaning_ja": "営業中",
        "hint_ja": "オーピズ",
        "parts": [],
    },
    {
        "id": "lokad",
        "stage": "signs",
        "text": "Lokað",
        "meaning": "closed",
        "meaning_ja": "閉店",
        "hint_ja": "ローカズ",
        "parts": [],
    },
    {
        "id": "gullfoss",
        "stage": "places",
        "text": "Gullfoss",
        "meaning": "Golden Falls",
        "meaning_ja": "黄金の滝",
        "hint_ja": "グットルフォス",
        "parts": [["gull", "金"], ["foss", "滝"]],
    },
    {
        "id": "own_1",
        "stage": "places",
        "text": "Testvík",
        "meaning": "a place on your trip",
        "meaning_ja": "あなたの旅程の地名",
        "own": True,
    },
]
QUESTIONS = [
    {"items": ["takk"], "prompt": "お礼を言って", "answer": "Takk."},
    {"items": ["bless"], "prompt": "さようなら", "answer": "Bless."},
]


class ReadingQueueTests(unittest.TestCase):
    def test_new_cards_in_deck_order_then_due_cards_first(self):
        q = ReadingQueue()
        self.assertEqual([c["id"] for c in q.select(DECK, D, 2)], ["opid", "lokad"])
        q.record("opid", "ok", D)
        q.record("lokad", "failed", D)
        tomorrow = D + timedelta(days=1)
        # both are due tomorrow: the failed one first, then the rest of the deck
        self.assertEqual(
            [c["id"] for c in q.select(DECK, tomorrow, 3)],
            ["lokad", "opid", "gullfoss"],
        )
        self.assertEqual(q.select(DECK, D, 0), [])

    def test_intervals_grow_with_a_streak(self):
        q = ReadingQueue()
        q.record("opid", "ok", D)
        q.record("opid", "ok", D + timedelta(days=1))
        self.assertEqual(q.cards["opid"].due, (D + timedelta(days=4)).isoformat())
        self.assertEqual(q.cards["opid"].streak, 2)
        q.record("opid", "failed", D + timedelta(days=4))
        self.assertEqual(q.cards["opid"].streak, 1)
        with self.assertRaises(ValueError):
            q.record("opid", "maybe", D)

    def test_cards_gone_from_the_deck_are_not_shown(self):
        q = ReadingQueue()
        q.record("old_card", "failed", D)
        self.assertNotIn(
            "old_card", [c["id"] for c in q.select(DECK, D + timedelta(days=5), 9)]
        )

    def test_saved_file_holds_ids_and_schedule_only(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "reading_queue.json"
            q = ReadingQueue()
            q.record("own_1", "shaky", D)
            q.save(path)
            text = path.read_text("utf-8")
            self.assertNotIn("Testvík", text)
            self.assertEqual(ReadingQueue.load(path).cards, q.cards)
            path.write_text("{broken", "utf-8")
            self.assertEqual(ReadingQueue.load(path).cards, {})
            self.assertTrue(path.with_name("reading_queue.json.broken").exists())

    def test_parse_deck_drops_malformed_cards(self):
        out = json.dumps(DECK + [{"id": "x"}, "junk", {"text": "no id"}])
        self.assertEqual([c["id"] for c in parse_deck(out)], [c["id"] for c in DECK])
        with self.assertRaises(ValueError):
            parse_deck('{"not": "a list"}')

    def test_render_hides_the_meaning_until_revealed(self):
        hidden = render_card(DECK[2], 1, 3, revealed=False, speak=True)
        self.assertIn("## Gullfoss", hidden)
        self.assertNotIn("黄金の滝", hidden)
        shown = render_card(DECK[2], 1, 3, revealed=True, speak=True)
        self.assertIn("意味: 黄金の滝（Golden Falls）", shown)
        self.assertIn("読み方の目安: グットルフォス", shown)
        self.assertIn("成り立ち: gull（金） + foss（滝）", shown)
        self.assertIn("🔊", shown)
        self.assertNotIn("🔊", render_card(DECK[2], 1, 3, revealed=True, speak=False))

    def test_profile_voice(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "p.toml"
            p.write_text(
                '[speakers]\nnative_a = { voice = "is-IS-GunnarNeural" }\n', "utf-8"
            )
            self.assertEqual(profile_voice(p), "is-IS-GunnarNeural")
            self.assertEqual(
                profile_voice(Path(td) / "missing.toml"), "is-IS-GudrunNeural"
            )


def session_with_cards(td, cards=DECK[:2]):
    path = Path(td) / "pending_review.json"
    queue = ReviewQueue.load(path, D)
    queue.add_from_plan({"lesson_number": 1, "new_items": [], "review": QUESTIONS}, D)
    keys = queue.select(D, 10)
    return ReviewSession(
        queue,
        keys,
        path,
        D,
        list(cards),
        ReadingQueue(),
        Path(td) / "reading_queue.json",
    )


class SessionTests(unittest.TestCase):
    def test_cards_come_after_the_questions_and_are_kept_apart(self):
        with tempfile.TemporaryDirectory() as td:
            s = session_with_cards(td)
            self.assertEqual(s.total, 4)
            self.assertIn("読みカードが2枚", s.render())
            s.rate("failed")
            s.rate("ok")
            self.assertEqual(s.current_card()["id"], "opid")
            self.assertIn("**読み 1/2**", s.render())
            s.rate("failed")
            s.rate("shaky")
            self.assertTrue(s.done)
            self.assertEqual(s.failed_ids(), ["takk"], "cards are not curriculum items")
            saved = ReadingQueue.load(s.reading_path).cards
            self.assertEqual(
                (saved["opid"].state, saved["lokad"].state), ("failed", "shaky")
            )
            states = sorted(
                e.state for e in ReviewQueue.load(s.path, D).entries.values()
            )
            self.assertEqual(states, ["failed", "ok"])
            summary = s.summary()
            self.assertIn("2/2問に回答", summary)
            self.assertIn("**読み**: 2/2枚（迷った 1・言えなかった 1）", summary)

    def test_unanswered_cards_are_not_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            s = session_with_cards(td)
            s.rate("ok")
            s.rate("ok")
            s.rate("ok")
            self.assertEqual(list(ReadingQueue.load(s.reading_path).cards), ["opid"])
            self.assertNotIn("未回答", s.summary())


class FakeInteraction:
    def __init__(self, user_id=1):
        self.user = SimpleNamespace(id=user_id)
        self.edits, self.followups, self.deferred = [], [], []
        it = self

        class Response:
            async def edit_message(self, content=None, view="unchanged"):
                it.edits.append((content, view))

            async def send_message(self, content, ephemeral=False):
                it.edits.append((content, ephemeral))

            async def defer(self, ephemeral=False, thinking=False):
                it.deferred.append(ephemeral)

        class Followup:
            async def send(self, content=None, file=None, ephemeral=False):
                it.followups.append(
                    (content, file.filename if file else None, ephemeral)
                )
                if file:
                    file.close()

        self.response = Response()
        self.followup = Followup()


class SpeakButtonTests(unittest.TestCase):
    def test_speak_only_on_a_revealed_card_and_only_to_the_learner(self):
        async def scenario(td):
            spoken = []

            @contextlib.asynccontextmanager
            async def speak(card):
                spoken.append(card["id"])
                mp3 = Path(td) / "x.mp3"
                mp3.write_bytes(b"ID3")
                yield mp3

            async def noop(*_):
                pass

            s = session_with_cards(td, DECK[2:3])
            view = ReviewView(s, 1, noop, noop, speak=speak)

            def labels():
                return {b.label: b for b in view.children}

            seen = []
            for step in ("答えを見る", "言えた", "答えを見る", "言えた", "答えを見る"):
                seen.append(sorted(labels()))
                await labels()[step].callback(FakeInteraction())
            seen.append(sorted(labels()))
            it = FakeInteraction()
            await labels()["🔊"].callback(it)
            self.assertEqual(spoken, ["gullfoss"])
            self.assertEqual(it.deferred, [True])
            self.assertEqual(it.followups, [(None, "reading.mp3", True)])
            self.assertEqual(len(s.results), 2, "listening does not advance the review")
            return seen

        with tempfile.TemporaryDirectory() as td:
            seen = asyncio.run(scenario(td))
        self.assertNotIn("🔊", seen[1], "no 🔊 on a question")
        self.assertEqual(seen[4], ["答えを見る"], "none before the card is revealed")
        self.assertIn("🔊", seen[5])

    def test_a_failed_synthesis_is_reported_privately(self):
        async def scenario(td):
            @contextlib.asynccontextmanager
            async def speak(card):
                raise OSError("no network")
                yield  # pragma: no cover

            async def noop(*_):
                pass

            s = session_with_cards(td, DECK[:1])
            s.rate("ok")
            s.rate("ok")
            view = ReviewView(s, 1, noop, noop, speak=speak)
            it = FakeInteraction()
            await view._speak(it)
            return it.followups

        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(
                asyncio.run(scenario(td)), [("音声を作れませんでした。", None, True)]
            )


class PickCardsTests(unittest.TestCase):
    def test_cards_never_crowd_out_every_question(self):
        async def scenario(td, limit, must_new):
            cfg = LessonConfig(root=Path(td), users={1: "yuki"}, review_limit=limit)
            cfg.user_dir("yuki").mkdir(exist_ok=True)
            queue = ReviewQueue()
            plan = {
                "lesson_number": 1,
                "new_items": [{"id": f"n{i}"} for i in range(must_new)],
                "review": [
                    {"items": [f"n{i}"], "prompt": "p", "answer": "a"}
                    for i in range(must_new)
                ],
            }
            queue.add_from_plan(plan, D)
            lessons = Lessons(cfg)

            async def deck(name):
                return list(DECK)

            lessons.reading_deck = deck
            _, cards = await lessons.pick_cards("yuki", queue, D)
            return len(cards)

        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(asyncio.run(scenario(td, 20, 3)), 4, "whole deck of 4")
            self.assertEqual(asyncio.run(scenario(td, 5, 0)), 4, "one question left")
            self.assertEqual(asyncio.run(scenario(td, 5, 3)), 2)
            self.assertEqual(asyncio.run(scenario(td, 3, 3)), 0)
            self.assertEqual(asyncio.run(scenario(td, 0, 3)), 4, "no limit")


class TripArgsTests(unittest.TestCase):
    def test_trip_is_passed_only_when_the_profile_exists(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = LessonConfig(root=Path(td), users={1: "yuki"})
            self.assertNotIn("--trip", cfg.generate_args("yuki"))
            cfg.user_dir("yuki").mkdir()
            cfg.trip_path("yuki").write_text("", "utf-8")
            args = cfg.generate_args("yuki")
            self.assertEqual(args[args.index("--trip") + 1], str(cfg.trip_path("yuki")))
            cfg.extra_args = ["--trip", "/elsewhere.toml"]
            self.assertEqual(cfg.generate_args("yuki").count("--trip"), 1)

    def test_question_limit_leaves_room_for_the_cards(self):
        cfg = LessonConfig(root=Path("/x"), users={1: "y"})
        self.assertEqual(
            (cfg.review_limit, cfg.reading_cards, cfg.question_limit), (20, 5, 15)
        )
        cfg.reading_cards = 0
        self.assertEqual(cfg.question_limit, 20)
        cfg.review_limit, cfg.reading_cards = 0, 5
        self.assertEqual(cfg.question_limit, 0)


if __name__ == "__main__":
    unittest.main()
