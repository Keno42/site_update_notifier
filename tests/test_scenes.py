"""src/scenes.py と、振り返りの場面カードのテスト. 実行: python -m unittest discover -s tests -t ."""

import asyncio
import contextlib
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from src.cards import CardQueue
from src.lesson import LessonConfig, Lessons
from src.review import ReviewSession, ReviewView
from src.review_queue import ReviewQueue
from src.scenes import parse_scenes, readiness, render_scene, speak_target

D = date(2026, 9, 26)
RESPOND = {
    "id": "a3_bag", "scenario": "A3", "kind": "respond", "tier": "A", "title_ja": "スーパー",
    "situation_ja": "レジで店員に何か聞かれました。袋は持っています。",
    "partner": "Viltu poka?", "partner_meaning_ja": "袋はいりますか？",
    "replies": ["Nei, takk."], "items": ["nei_takk"], "note_ja": "«poka» は「袋」。",
}  # fmt: skip
INITIATE = {
    "id": "a8_toilet", "scenario": "A8", "kind": "initiate", "tier": "A",
    "title_ja": "すみません・トイレ", "situation_ja": "トイレに行きたいです。",
    "partner": "", "replies": ["Hvar er klósettið?"], "items": ["hvar_er", "klosettid"],
}  # fmt: skip
QUESTIONS = [
    {"items": ["takk"], "prompt": "お礼を言って", "answer": "Takk."},
]


class RenderTests(unittest.TestCase):
    def test_the_partners_words_are_heard_first_and_read_after(self):
        before = render_scene(RESPOND, 1, 2, revealed=False, speak=True)
        self.assertIn("**場面 1/2**（スーパー）", before)
        self.assertIn("レジで店員に何か聞かれました", before)
        self.assertIn("🔊 で相手の言葉を聞いて", before)
        self.assertNotIn("Viltu poka?", before, "heard, not read, before answering")
        self.assertNotIn("Nei, takk.", before)
        after = render_scene(RESPOND, 1, 2, revealed=True, speak=True)
        self.assertIn("相手: «Viltu poka?» — 袋はいりますか？", after)
        self.assertIn("答えの例: Nei, takk.", after)
        self.assertIn("メモ: «poka» は「袋」。", after)

    def test_without_speech_the_partner_line_is_shown(self):
        before = render_scene(RESPOND, 1, 1, revealed=False, speak=False)
        self.assertIn("相手: «Viltu poka?»", before)
        self.assertNotIn("袋はいりますか", before)

    def test_an_initiate_card_has_no_partner(self):
        before = render_scene(INITIATE, 1, 1, revealed=False, speak=True)
        self.assertIn("声に出して言ってから", before)
        self.assertNotIn(
            "相手", render_scene(INITIATE, 1, 1, revealed=True, speak=True)
        )

    def test_what_the_speaker_button_plays(self):
        self.assertEqual(speak_target(RESPOND, False), {"text": "Viltu poka?"})
        self.assertEqual(speak_target(RESPOND, True), {"text": "Nei, takk."})
        self.assertIsNone(speak_target(INITIATE, False))
        self.assertEqual(speak_target(INITIATE, True), {"text": "Hvar er klósettið?"})

    def test_parse_drops_malformed_cards(self):
        out = json.dumps([RESPOND, {"id": "x"}, "junk", INITIATE])
        self.assertEqual([c["id"] for c in parse_scenes(out)], ["a3_bag", "a8_toilet"])
        with self.assertRaises(ValueError):
            parse_scenes("{}")


class ReadinessTests(unittest.TestCase):
    def test_ready_practising_and_untaught(self):
        cards = [
            {**RESPOND, "id": "a3_bag"},
            {**RESPOND, "id": "a3_receipt"},
            {**INITIATE, "id": "a8_toilet"},
            {**INITIATE, "id": "a1_enter", "scenario": "A1", "title_ja": "挨拶"},
            {
                **INITIATE,
                "id": "b1_photo",
                "scenario": "B1",
                "tier": "B",
                "title_ja": "博物館",
            },
        ]
        q = CardQueue()
        q.record("a1_enter", "ok", D)
        q.record("a3_bag", "ok", D)
        text = readiness(cards, {"a1_enter", "a3_bag", "a3_receipt"}, q)
        self.assertIn("Tier A（3場面）: 準備OK 1・練習中 1・未学習 1", text)
        self.assertIn("準備OK: 挨拶", text)
        self.assertIn("Tier B（1場面）: 準備OK 0・練習中 0・未学習 1", text)
        self.assertEqual(readiness([], set(), q), "")


def session_with(td, scenes, cards=()):
    path = Path(td) / "pending_review.json"
    queue = ReviewQueue.load(path, D)
    queue.add_from_plan({"lesson_number": 1, "new_items": [], "review": QUESTIONS}, D)
    return ReviewSession(
        queue, queue.select(D, 10), path, D, list(cards), CardQueue(),
        Path(td) / "reading_queue.json", scenes=list(scenes),
        scene_queue=CardQueue(), scene_path=Path(td) / "scene_queue.json",
    )  # fmt: skip


class SessionTests(unittest.TestCase):
    def test_scenes_come_after_the_questions_and_before_the_reading_cards(self):
        card = {"id": "opid", "stage": "signs", "text": "Opið", "meaning": "Open"}
        with tempfile.TemporaryDirectory() as td:
            s = session_with(td, [RESPOND, INITIATE], [card])
            self.assertEqual(s.total, 4)
            self.assertIn(
                "続けて場面カードが2枚、読みカードが1枚あります", s.render(speak=True)
            )
            s.rate("ok")
            self.assertEqual(s.current_scene()["id"], "a3_bag")
            s.rate("failed")
            s.rate("shaky")
            self.assertEqual(s.current_card()["id"], "opid")
            s.rate("ok")
            self.assertTrue(s.done)
            saved = CardQueue.load(s.scene_path).cards
            self.assertEqual(
                (saved["a3_bag"].state, saved["a8_toilet"].state), ("failed", "shaky")
            )
            self.assertEqual(list(CardQueue.load(s.reading_path).cards), ["opid"])
            # the reading card answered ok comes back the next day; a scene card would wait three days (#95)
            self.assertEqual(CardQueue.load(s.reading_path).cards["opid"].due, (D + timedelta(days=1)).isoformat())
            self.assertEqual(s.failed_ids(), [], "scene cards are not curriculum items")
            summary = s.summary()
            self.assertIn("**場面**: 2/2枚（迷った 1・言えなかった 1）", summary)
            self.assertIn("**読み**: 1/1枚（言えた 1）", summary)


class FakeInteraction:
    def __init__(self):
        self.user = SimpleNamespace(id=1)
        self.followups, self.deferred = [], []
        it = self

        class Response:
            async def edit_message(self, content=None, view="unchanged"):
                pass

            async def defer(self, ephemeral=False, thinking=False):
                it.deferred.append(ephemeral)

        class Followup:
            async def send(self, content=None, file=None, ephemeral=False):
                it.followups.append((content, ephemeral))
                if file:
                    file.close()

        self.response = Response()
        self.followup = Followup()


class SpeakerTests(unittest.TestCase):
    def test_a_scene_card_answered_well_the_first_time_comes_back_in_three_days(self):
        with tempfile.TemporaryDirectory() as td:
            s = session_with(td, [RESPOND, INITIATE])
            s.rate("ok")
            s.rate("ok")
            s.rate("ok")  # the two questions, then the scene card
            s.rate("ok")
            saved = CardQueue.load(s.scene_path).cards
            self.assertEqual({k: v.due for k, v in saved.items()}, {k: (D + timedelta(days=3)).isoformat() for k in saved})
            self.assertEqual(len(saved), 2)

    def test_a_respond_card_plays_the_partner_before_and_the_reply_after(self):
        async def scenario(td):
            spoken = []

            @contextlib.asynccontextmanager
            async def speak(card):
                spoken.append(card["text"])
                mp3 = Path(td) / "x.mp3"
                mp3.write_bytes(b"ID3")
                yield mp3

            async def noop(*_):
                pass

            s = session_with(td, [RESPOND, INITIATE])
            view = ReviewView(s, 1, noop, noop, speak=speak)

            def labels():
                return {b.label: b for b in view.children}

            seen = [sorted(labels())]  # the question
            await labels()["答えを見る"].callback(FakeInteraction())
            await labels()["言えた"].callback(FakeInteraction())
            seen.append(sorted(labels()))  # the respond card, before the answer
            await labels()["🔊"].callback(FakeInteraction())
            await labels()["答えを見る"].callback(FakeInteraction())
            await labels()["🔊"].callback(FakeInteraction())
            await labels()["言えた"].callback(FakeInteraction())
            seen.append(sorted(labels()))  # the initiate card, before the answer
            return seen, spoken

        with tempfile.TemporaryDirectory() as td:
            seen, spoken = asyncio.run(scenario(td))
        self.assertNotIn("🔊", seen[0], "no 🔊 on a question")
        self.assertIn("🔊", seen[1], "the partner is heard before answering")
        self.assertEqual(spoken, ["Viltu poka?", "Nei, takk."])
        self.assertEqual(
            seen[2], ["答えを見る"], "nothing to hear before an initiate answer"
        )


class PickTests(unittest.TestCase):
    def test_scenes_take_the_slot_first_and_the_review_keeps_its_length(self):
        async def scenario(td, limit, must_new):
            cfg = LessonConfig(
                root=Path(td), users={1: "yuki"}, review_limit=limit,
                scene_cards=3, reading_cards=3,
            )  # fmt: skip
            cfg.user_dir("yuki").mkdir(exist_ok=True)
            queue = ReviewQueue()
            queue.add_from_plan(
                {
                    "lesson_number": 1,
                    "new_items": [{"id": f"n{i}"} for i in range(must_new)],
                    "review": [
                        {"items": [f"n{i}"], "prompt": "p", "answer": "a"}
                        for i in range(must_new)
                    ],
                },
                D,
            )
            lessons = Lessons(cfg)

            async def scene_deck(name, channel=None, learner=True):
                return [dict(RESPOND, id=f"s{i}") for i in range(5)]

            async def reading_deck(name, channel=None):
                return [{"id": f"r{i}", "text": f"T{i}"} for i in range(5)]

            lessons.scene_deck = scene_deck
            lessons.reading_deck = reading_deck
            _, picked = await lessons.pick_scenes("yuki", queue, D)
            _, cards = await lessons.pick_cards("yuki", queue, D, reserved=len(picked))
            return len(picked), len(cards)

        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(asyncio.run(scenario(td, 20, 3)), (3, 3))
            self.assertEqual(asyncio.run(scenario(td, 20, 16)), (3, 1))
            self.assertEqual(
                asyncio.run(scenario(td, 5, 0)), (3, 1), "one question left"
            )
            self.assertEqual(asyncio.run(scenario(td, 3, 3)), (0, 0))


class ReadinessCadenceTests(unittest.TestCase):
    def test_the_summary_comes_once_per_interval(self):
        async def scenario(td):
            cfg = LessonConfig(root=Path(td), users={1: "yuki"}, readiness_days=7)
            cfg.user_dir("yuki").mkdir()
            lessons = Lessons(cfg)

            async def deck(name, source, learner=True):
                return [INITIATE] if learner else [INITIATE, RESPOND]

            lessons._scene_deck = deck
            out = []
            for days in (0, 3, 7):
                out.append(
                    await lessons.readiness_summary(
                        "yuki", None, D + timedelta(days=days)
                    )
                )
            return out

        with tempfile.TemporaryDirectory() as td:
            first, soon, week = asyncio.run(scenario(td))
        self.assertIn("旅行の準備（場面カード）", first)
        self.assertEqual(soon, "")
        self.assertIn("旅行の準備", week)


if __name__ == "__main__":
    unittest.main()
