"""language-learning-audio #428 (the owner, 2026-11-25: a /lesson-configure option): the learner's own name and age reach the generation, and
the feedback repository gets the persona's values back."""
import asyncio
import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace

from src import feedback, feedback_sync, own, usersettings
from src.lesson import Lessons
from tests.test_feedback_sync import FakeGitHub, write_record
from tests.test_usersettings import cfg_in

LLA = Path(__file__).resolve().parent.parent / "external" / "language-learning-audio"


def interaction(replies, user=1):
    class Response:
        async def send_message(self, content, ephemeral=False, view=None):
            replies.append((content, ephemeral))

    return SimpleNamespace(user=SimpleNamespace(id=user), response=Response())


class OwnTests(unittest.TestCase):
    def test_the_values_are_set_cleared_and_checked_privately(self):
        with tempfile.TemporaryDirectory() as td:
            lessons = Lessons(cfg_in(td))
            replies = []
            asyncio.run(lessons.configure(interaction(replies), own_name="Kenji", own_age=25))
            settings = lessons.cfg.user_settings("yuki")
            self.assertEqual((settings.own_name, settings.own_age), ("Kenji", 25))
            self.assertIn("Kenji", replies[-1][0])
            self.assertTrue(replies[-1][1], "answered to the learner only")
            self.assertEqual(own.generate_args(settings.own_name, settings.own_age), ["--own-name", "Kenji", "--own-age", "25"])
            asyncio.run(lessons.configure(interaction(replies), own_name="Ken 2"))
            self.assertIn("1語", replies[-1][0])
            self.assertEqual(lessons.cfg.user_settings("yuki").own_name, "Kenji", "a bad name changes nothing")
            asyncio.run(lessons.configure(interaction(replies), own_name="-", own_age=0))
            settings = lessons.cfg.user_settings("yuki")
            self.assertEqual((settings.own_name, settings.own_age), (None, None))
            self.assertEqual(own.generate_args(None, None), [], "unset: generated as before")

    def test_the_arguments_reach_the_generation_only_when_set(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = cfg_in(td)
            args = cfg.generate_args("yuki", False, own_args=own.generate_args("Kenji", 30))
            self.assertEqual(args[-4:], ["--own-name", "Kenji", "--own-age", "30"])
            self.assertNotIn("--own-name", cfg.generate_args("yuki", False))

    def test_the_persona_comes_back_before_a_record_leaves(self):
        text = ('"generate_args": ["generate", "--own-name", "Kenji", "--own-age", "25"]\n**You:** Ég heiti Kenji.\n'
                "**Instructor:** Say you're 25.\n**You:** Ég er tuttugu og fimm ára.\n**Speaker A:** Takk! Ég er þrjátíu ára. En þú?\nKenjiro stays.")
        out = own.scrub(text, "Kenji", 25)
        for personal in ('"Kenji"', "heiti Kenji", "you're 25", "tuttugu og fimm", '"25"'):
            self.assertNotIn(personal, out)
        self.assertIn("Ég heiti Yuki.", out)
        self.assertIn("Ég er tuttugu ára.", out)
        self.assertIn("Ég er þrjátíu ára.", out, "her age is hers")
        self.assertIn("Kenjiro stays.", out, "only the name as a word")

    def test_the_sync_scrubs_the_learner_s_own_values(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            record = write_record(root / "owner", 70, ["generate", "--own-name", "Kenji", "--own-age", "25"])
            (root / "owner" / feedback.MANIFESTS / record / "lesson-070.transcript.md").write_text("**You:** Ég heiti Kenji. Ég er tuttugu og fimm ára.", "utf-8")
            usersettings.save(root / "owner", usersettings.UserSettings(30, "before", "spread", own_name="Kenji", own_age=25))
            cfg = feedback_sync.SyncConfig(repo="o/r", root=root, users=["owner"], full={"owner"})
            gh = FakeGitHub()
            asyncio.run(feedback_sync.FeedbackSync(cfg, gh, delay=0).run())
            everything = "\n".join(gh.files.values())
            self.assertNotIn("Kenji", everything)
            self.assertNotIn("tuttugu og fimm", everything)
            self.assertIn("Ég heiti Yuki.", everything)

    def sync_files(self, root: Path, gh=None) -> tuple[FakeGitHub, str]:
        gh = gh or FakeGitHub()
        cfg = feedback_sync.SyncConfig(repo="o/r", root=root, users=["owner"], full={"owner"})
        asyncio.run(feedback_sync.FeedbackSync(cfg, gh, delay=0).run())
        return gh, "\n".join(gh.files.values())

    def test_a_record_is_scrubbed_with_its_own_values_whatever_the_settings_are_now(self):
        """Review of #245: the sync read the learner's current settings, so a lesson made with «Kenji» was scrubbed with the new value (or none)
        once the setting changed or was cleared, and the changed digest pushed it again with the name in it."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            record = write_record(root / "owner", 70, ["generate", "--own-name", "Kenji", "--own-age", "25"])
            d = root / "owner" / feedback.MANIFESTS / record
            (d / "lesson-070.transcript.md").write_text("**You:** Ég heiti Kenji. Ég er tuttugu og fimm ára.\n**Instructor:** Say you're 25.", "utf-8")
            usersettings.save(root / "owner", usersettings.UserSettings(30, "before", "spread", own_name="Kenji", own_age=25))
            gh, everything = self.sync_files(root)
            for settings in (usersettings.UserSettings(30, "before", "spread", own_name="Taro", own_age=30), usersettings.UserSettings(30, "before", "spread")):
                usersettings.save(root / "owner", settings)
                gh, everything = self.sync_files(root, gh)
                for personal in ("Kenji", "tuttugu og fimm", "you're 25", '"25"'):
                    self.assertNotIn(personal, everything, (settings.own_name, personal))
                self.assertIn("Ég heiti Yuki.", everything)
            later = write_record(root / "owner", 71, ["generate", "--own-name", "Taro"])
            (root / "owner" / feedback.MANIFESTS / later / "lesson-071.transcript.md").write_text("**You:** Ég heiti Taro.", "utf-8")
            _, everything = self.sync_files(root, gh)
            self.assertNotIn("Taro", everything, "each record with its own value")

    def test_a_name_beside_japanese_text_is_scrubbed(self):
        """Review of #245: «Kenjiの名前で、と言ってください»: a Japanese letter is a letter to ``\\w``, so the name's edge was never found."""
        self.assertEqual(own.scrub("Kenjiの名前で、と言ってください。私はケンジ。", "Kenji", None), "Yukiの名前で、と言ってください。私はケンジ。")
        self.assertEqual(own.scrub("Kenjiro, Kenji-san, Kenji.", "Kenji", None), "Kenjiro, Yuki-san, Yuki.")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            record = write_record(root / "owner", 72, ["generate", "--own-name", "Kenji", "--known", "ja"])
            (root / "owner" / feedback.MANIFESTS / record / "lesson-072.plan.json").write_text(
                json.dumps({"cue": "Kenjiの名前で、と言ってください。", "answer": "Á nafninu Kenji."}, ensure_ascii=False), "utf-8")
            _, everything = self.sync_files(root)
            self.assertNotIn("Kenji", everything)
            self.assertIn("Yukiの名前で", everything)

    def test_a_name_is_in_latin_letters(self):
        self.assertTrue(own.valid_name("Kenji") and own.valid_name("Ástríður") and own.valid_name("Mary-Ann"))
        for bad in ("健二", "Ken 2", "K3n", "-"):
            self.assertFalse(own.valid_name(bad), bad)
        self.assertEqual(own.values_of_args(["generate", "--own-name", "Kenji", "--own-age", "25"]), ("Kenji", 25))
        self.assertEqual(own.values_of_args(["generate"]), (None, None))

    def test_the_ages_are_those_the_course_can_teach(self):
        """``own.AGE_WORDS`` is the bot's copy of the course's age fillers (language-learning-audio's items tagged «age»)."""
        found = {}
        for path in sorted((LLA / "curricula" / "is-en").glob("*.toml")):
            for item in tomllib.loads(path.read_text("utf-8")).get("items", []):
                if "age" in item.get("tags", []):
                    found[item["target"]] = item["meaning"]
        if not found:
            self.skipTest("the submodule is not checked out")
        self.assertEqual(set(own.AGE_WORDS.values()), set(found))
        self.assertEqual(own.AGE_WORDS[own.PERSONA_AGE], "tuttugu")


if __name__ == "__main__":
    unittest.main()
