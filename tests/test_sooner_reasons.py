"""language-learning-audio #444 (lesson 42): «lögreglustöðin», reported hard to *hear*, got «This word is hard to say.»: the form recorded the
reasons but the report carried only the ids. They now reach `report --sooner-reasons`, deferred reports keep them, and a «sooner» with no reason is
reported as before (the audio lesson then gives the pronunciation model, as it always has)."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from src.feedback import Feedback
from src.lesson import LessonConfig, Lessons
from tests.test_feedback import NOW, interaction, saved, submit_form


class SoonerReasonsTests(unittest.TestCase):
    def test_report_args_name_the_reasons_only_with_items_and_reasons(self):
        cfg = LessonConfig(root=Path("/nonexistent"), users={1: "yuki"})
        args = cfg.report_args("yuki", [], lesson=3, sooner=["a"], sooner_reasons=["hear"])
        self.assertEqual(args[args.index("--sooner-reasons") + 1], "hear")
        self.assertNotIn("--sooner-reasons", cfg.report_args("yuki", [], lesson=3, sooner=["a"], sooner_reasons=[]))
        self.assertNotIn("--sooner-reasons", cfg.report_args("yuki", [], lesson=3, sooner=[], load="light", sooner_reasons=["hear"]))

    def test_the_form_passes_its_reasons(self):
        calls = []
        with tempfile.TemporaryDirectory() as td:
            saved(Path(td))

            async def report(name, lesson, ids, load, reasons=None):
                calls.append((ids, reasons))
                return True

            fb = Feedback({1: "yuki"}, lambda n: Path(td) / n, 0, lambda: NOW, report_feedback=report)

            async def run():
                log = []
                await fb.open_form(interaction(log))
                kw = log[-1][2]
                selects = [c for c in kw["view"].children if hasattr(c, "options")]
                selects[0]._values = ["takk_fyrir"]
                await selects[0].callback(interaction(log))
                selects[1]._values = ["hear"]
                await selects[1].callback(interaction(log))
                await submit_form(self, log, kw, "right")

            asyncio.run(run())
        self.assertEqual(calls, [(["takk_fyrir"], ["hear"])])

    def test_a_deferred_report_keeps_its_reasons(self):
        cfg = LessonConfig(root=Path("/nonexistent"), users={1: "yuki"})
        lessons = Lessons(cfg)
        sent = []

        async def fake_run_cli(c, args):
            sent.append(args)
            return 0, "", ""

        async def run():
            lessons.busy["yuki"] = time.monotonic()
            self.assertTrue(await lessons.report_feedback("yuki", 3, ["a"], None, reasons=["hear", "say"]))
            self.assertEqual(sent, [])
            await lessons.release("yuki")

        with mock.patch("src.lesson.run_cli", fake_run_cli):
            asyncio.run(run())
        self.assertEqual(sent[0][sent[0].index("--sooner-reasons") + 1], "hear,say")


if __name__ == "__main__":
    unittest.main()
