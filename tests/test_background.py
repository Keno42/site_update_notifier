"""language-learning-audio #625: the asides' background (audiolesson's --background). This bot speaks Japanese, so a user with no setting is
taught as a Japanese speaker («日本»: the asides that compare with Japan play as written); «なし» leaves --background out, and the asides play
their version with no comparison. Changeable with /lesson-configure."""

import json
import tempfile
import unittest
from pathlib import Path

from src import usersettings
from src.lesson import LessonConfig


def cfg_in(td):
    return LessonConfig(root=Path(td), users={1: "yuki"})


class BackgroundSettingTests(unittest.TestCase):
    def test_no_setting_is_japan_and_a_choice_is_kept(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = cfg_in(td)
            settings = cfg.user_settings("yuki")
            self.assertIsNone(settings.background)
            self.assertEqual(settings.background_arg(), "ja")
            self.assertIn("豆知識の背景: 日本", settings.describe())
            settings.background = "none"
            usersettings.save(cfg.user_dir("yuki"), settings)
            again = cfg.user_settings("yuki")
            self.assertEqual((again.background, again.background_arg()), ("none", None))
            self.assertIn("豆知識の背景: なし", again.describe())
            (cfg.user_dir("yuki") / usersettings.SETTINGS_FILE).write_text(json.dumps({"background": "fr"}), "utf-8")
            self.assertIsNone(cfg.user_settings("yuki").background, "an unknown value is no choice: Japan")

    def test_generate_passes_it_only_when_there_is_one(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = cfg_in(td)
            args = cfg.generate_args("yuki", background="ja")
            self.assertEqual(args[args.index("--background") + 1], "ja")
            self.assertNotIn("--background", cfg.generate_args("yuki"), "None: not passed")


if __name__ == "__main__":
    unittest.main()
