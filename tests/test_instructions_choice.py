"""language-learning-audio #429 (owner, 2026-11-14): replacing the lessons' instructions with Icelandic ones is the learner's choice. The bot asks
it once, at the start of a lesson once INSTRUCTIONS_ASK_FROM lessons are done: «段階的に進める» or «置き換えない», changeable with
/lesson-configure. Not answered, the lesson is generated with --instructions pending."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from src import usersettings
from src.lesson import INSTRUCTIONS_QUESTION, InstructionsView, LessonConfig, Lessons
from src.review_queue import ReviewQueue
from tests.test_lesson import FakeChannel
from tests.test_review_queue import D, plan


def cfg_in(td):
    return LessonConfig(root=Path(td), users={1: "yuki"})


class InstructionsSettingTests(unittest.TestCase):
    def test_not_answered_is_pending_and_an_answer_is_kept(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = cfg_in(td)
            settings = cfg.user_settings("yuki")
            self.assertIsNone(settings.instructions)
            self.assertEqual(settings.instructions_arg(), "pending")
            self.assertIn("アイスランド語の指示: 未回答", settings.describe())
            settings.instructions = "off"
            usersettings.save(cfg.user_dir("yuki"), settings)
            again = cfg.user_settings("yuki")
            self.assertEqual((again.instructions, again.instructions_arg()), ("off", "off"))
            self.assertIn("アイスランド語の指示: 置き換えない", again.describe())
            (cfg.user_dir("yuki") / usersettings.SETTINGS_FILE).write_text(json.dumps({"instructions": "sometimes"}), "utf-8")
            self.assertIsNone(cfg.user_settings("yuki").instructions, "an unknown value is not an answer")

    def test_generate_passes_the_choice(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = cfg_in(td)
            args = cfg.generate_args("yuki", instructions="pending")
            self.assertEqual(args[args.index("--instructions") + 1], "pending")
            self.assertNotIn("--instructions", cfg.generate_args("yuki"), "None: not passed")


class AskOnceTests(unittest.TestCase):
    def ask(self, td, done: int, answer: str | None = None) -> FakeChannel:
        cfg = cfg_in(td)
        lessons = Lessons(cfg)
        d = cfg.user_dir("yuki")
        d.mkdir(parents=True, exist_ok=True)
        (d / "learner.json").write_text(json.dumps({"lessons_completed": done}), "utf-8")
        if answer:
            lessons.set_instructions("yuki", answer)
        channel = FakeChannel()
        asyncio.run(lessons.ask_instructions(channel, 1, "yuki"))
        return channel

    def test_asked_from_the_tenth_lesson_until_answered(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(self.ask(td, 9).sent, [], "before: not asked")
        with tempfile.TemporaryDirectory() as td:
            sent = self.ask(td, usersettings.INSTRUCTIONS_ASK_FROM).sent
            self.assertEqual([c for c, _ in sent], [INSTRUCTIONS_QUESTION])
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(self.ask(td, 56).sent[0][0], INSTRUCTIONS_QUESTION, "a learner past the tenth lesson is asked too")
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(self.ask(td, 56, "gradual").sent, [], "answered: never asked again")

    def test_a_button_saves_the_answer(self):
        class Response:
            def __init__(self):
                self.edited = None

            async def edit_message(self, content=None, view="unset"):
                self.edited = (content, view)

            async def send_message(self, *a, **kw):
                raise AssertionError("the owner may press it")

        class Interaction:
            def __init__(self, uid):
                self.user = type("U", (), {"id": uid})()
                self.response = Response()

        async def press():
            with tempfile.TemporaryDirectory() as td:
                lessons = Lessons(cfg_in(td))
                view = InstructionsView(lessons, 1, "yuki")
                interaction = Interaction(1)
                await view.choose(interaction, "off")
                self.assertEqual(lessons.cfg.user_settings("yuki").instructions, "off")
                self.assertIn("「置き換えない」にしました", interaction.response.edited[0])
                self.assertIsNone(interaction.response.edited[1], "the buttons go")

        asyncio.run(press())


class WithdrawnCardsTests(unittest.TestCase):
    def test_a_withdrawn_items_cards_leave_the_queue(self):
        with tempfile.TemporaryDirectory() as td:
            q = ReviewQueue.load(Path(td) / "q.json", D)
            q.add_from_plan(plan(7, ["endurtaktu", "a"], [["endurtaktu"], ["a"]]), D)
            self.assertEqual(len(q.entries), 2)
            p = plan(8, [], [])
            p["withdrawn_items"] = ["endurtaktu", "hlustadu"]
            q.add_from_plan(p, D)
            self.assertEqual([e.items for e in q.entries.values()], [["a"]])


if __name__ == "__main__":
    unittest.main()
