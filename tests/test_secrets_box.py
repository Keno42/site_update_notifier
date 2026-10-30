"""Keno42/site_update_notifier#119: secrets from the repository's Actions secrets, sealed in CI and opened on the bot's machine."""
import asyncio
import importlib.util
import io
import json
import logging
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src import lesson, newlist, secrets_box, seal_secrets, speech

VALUES = {"AZURE_SPEECH_KEY": "SECRET-KEY-123", "AZURE_SPEECH_REGION": "northeurope", "OTHER": "not allowed"}


@unittest.skipUnless(importlib.util.find_spec("nacl"), "PyNaCl is not installed")
class KeyTests(unittest.TestCase):
    def test_the_key_is_created_once_with_mode_600_and_reloaded(self):
        with tempfile.TemporaryDirectory() as td:
            key = secrets_box.load_or_create_key(Path(td))
            path = Path(td) / secrets_box.KEY_FILE
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            again = secrets_box.load_or_create_key(Path(td))
            self.assertEqual(bytes(key), bytes(again))
            self.assertEqual(secrets_box.public_key_b64(key), secrets_box.public_key_b64(again))


@unittest.skipUnless(importlib.util.find_spec("nacl"), "PyNaCl is not installed")
class UnwritableRootTests(unittest.TestCase):
    def test_a_root_that_cannot_be_written_only_warns(self):
        with mock.patch.object(Path, "mkdir", side_effect=PermissionError(13, "Permission denied")), \
                self.assertLogs(level="WARNING") as logs:
            self.assertEqual(secrets_box.load(Path("/nonexistent-root")), {})
        self.assertEqual(len(logs.output), 1)


@unittest.skipUnless(importlib.util.find_spec("nacl"), "PyNaCl is not installed")
class SealTests(unittest.TestCase):
    def test_a_sealed_value_opens_to_the_allowlisted_names_only(self):
        with tempfile.TemporaryDirectory() as td:
            key = secrets_box.load_or_create_key(Path(td))
            sealed = secrets_box.seal(VALUES, secrets_box.public_key_b64(key))
            self.assertNotIn("SECRET-KEY-123", sealed)
            self.assertEqual(secrets_box.open_sealed(sealed, key), {"AZURE_SPEECH_KEY": "SECRET-KEY-123", "AZURE_SPEECH_REGION": "northeurope"})

    def test_a_wrong_key_or_a_corrupt_file_does_not_open(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            key_a, key_b = secrets_box.load_or_create_key(Path(a)), secrets_box.load_or_create_key(Path(b))
            sealed = secrets_box.seal(VALUES, secrets_box.public_key_b64(key_a))
            with self.assertRaises(ValueError):
                secrets_box.open_sealed(sealed, key_b)
            with self.assertRaises(ValueError):
                secrets_box.open_sealed("not base64 !!", key_a)

    def test_load_puts_the_values_in_memory_only_and_ignores_a_bad_file_with_one_warning(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            key = secrets_box.load_or_create_key(root)
            sealed_path = root / "secrets.sealed"
            sealed_path.write_text(secrets_box.seal(VALUES, secrets_box.public_key_b64(key)), "ascii")
            before = dict(os.environ)
            secrets_box.load(root, sealed_path)
            self.assertEqual(secrets_box.SECRETS["AZURE_SPEECH_REGION"], "northeurope")
            self.assertEqual(dict(os.environ), before, "not into os.environ")
            self.assertEqual(secrets_box.status(), "secrets: azure ✓")
            sealed_path.write_text("garbage", "ascii")
            with self.assertLogs(level="WARNING") as logs:
                secrets_box.load(root, sealed_path)
            self.assertEqual(secrets_box.SECRETS, {})
            self.assertEqual(len(logs.output), 1)
            self.assertNotIn("SECRET", logs.output[0])
            self.assertEqual(secrets_box.status(), "secrets: azure –")
            secrets_box.load(root, root / "none.sealed")  # a missing file is no warning
            self.assertEqual(secrets_box.SECRETS, {})


@unittest.skipUnless(importlib.util.find_spec("nacl"), "PyNaCl is not installed")
class WorkflowStepTests(unittest.TestCase):
    def test_the_sealing_step_opens_back_to_the_same_json_and_prints_no_value(self):
        with tempfile.TemporaryDirectory() as td:
            key = secrets_box.load_or_create_key(Path(td))
            out = Path(td) / "sealed" / "secrets.sealed"
            env = {"BOT_PUBLIC_KEY": secrets_box.public_key_b64(key), **VALUES}
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf), mock.patch("sys.stderr", buf):
                rc = seal_secrets.main([str(out)], env)
            self.assertEqual(rc, 0)
            self.assertNotIn("SECRET-KEY-123", buf.getvalue())
            self.assertEqual(secrets_box.open_sealed(out.read_text("ascii"), key), {"AZURE_SPEECH_KEY": "SECRET-KEY-123", "AZURE_SPEECH_REGION": "northeurope"})

    def test_a_missing_public_key_or_secret_fails_naming_only_the_name(self):
        buf = io.StringIO()
        with mock.patch("sys.stderr", buf):
            self.assertEqual(seal_secrets.main(["x"], {}), 1)
            self.assertEqual(seal_secrets.main(["x"], {"BOT_PUBLIC_KEY": "AAAA", "AZURE_SPEECH_KEY": "SECRET"}), 1)
        self.assertIn("AZURE_SPEECH_REGION", buf.getvalue())
        self.assertNotIn("SECRET", buf.getvalue())


class SubprocessEnvTests(unittest.TestCase):
    def test_secrets_reach_the_subprocess_env_and_not_os_environ(self):
        secrets_box.SECRETS.clear()
        secrets_box.SECRETS.update({"AZURE_SPEECH_KEY": "SECRET-KEY-123", "AZURE_SPEECH_REGION": "northeurope"})
        self.addCleanup(secrets_box.SECRETS.clear)
        captured = {}

        async def fake_exec(*args, **kw):
            captured.update(kw)

            class Proc:
                stdout = stderr = None
                stdin = None
                returncode = 0

            raise RuntimeError("stop")

        cfg = SimpleNamespace(python="python", lla_dir=Path("."), timeout_min=1)
        with mock.patch("asyncio.create_subprocess_exec", fake_exec):
            with self.assertRaises(RuntimeError):
                asyncio.run(lesson.run_cli(cfg, ["x"], env=secrets_box.environment()))
        self.assertEqual(captured["env"]["AZURE_SPEECH_KEY"], "SECRET-KEY-123")
        self.assertNotIn("AZURE_SPEECH_KEY", os.environ)

    def test_without_secrets_the_environment_is_left_alone(self):
        captured = {}

        async def fake_exec(*args, **kw):
            captured.update(kw)
            raise RuntimeError("stop")

        cfg = SimpleNamespace(python="python", lla_dir=Path("."), timeout_min=1)
        with mock.patch("asyncio.create_subprocess_exec", fake_exec):
            with self.assertRaises(RuntimeError):
                asyncio.run(lesson.run_cli(cfg, ["x"]))
        self.assertIsNone(captured["env"])


class SpeechTests(unittest.TestCase):
    def test_the_default_tts_is_azure_only_with_the_key(self):
        secrets_box.SECRETS.clear()
        self.assertIs(speech.default_tts(), speech.edge_tts_save)
        secrets_box.SECRETS.update({"AZURE_SPEECH_KEY": "k", "AZURE_SPEECH_REGION": "r"})
        self.addCleanup(secrets_box.SECRETS.clear)
        self.assertIs(speech.default_tts(), speech.azure_tts_save)


class PublicKeyCommandTests(unittest.TestCase):
    def test_the_command_is_registered_ephemeral_and_for_registered_users_only(self):
        import discord

        client = discord.Client(intents=discord.Intents.none())
        with tempfile.TemporaryDirectory() as td:
            cfg = SimpleNamespace(LESSON_ROOT=td, LESSON_USERS={1: "yuki"})
            sent = []

            class Response:
                async def send_message(self, content, ephemeral=False):
                    sent.append((content, ephemeral))

            # the command is built inside setup(); read it back from the tree it registers on
            # setup() also installs module-wide hooks (newlist.list_view): restored after, so later tests see none (a new-list button that
            # sent its preview view broke NewListTimingTests' stand-in, in whole-suite order only)
            with mock.patch("src.lesson.app_commands.CommandTree") as Tree, mock.patch.object(newlist, "list_view", newlist.list_view):
                tree = Tree.return_value
                added = []
                tree.add_command.side_effect = lambda c, guild=None: added.append(c)
                lesson.setup(client, cfg)
            command = next(c for c in added if c.name == "bot-public-key")

            async def call(user_id):
                await command.callback(SimpleNamespace(user=SimpleNamespace(id=user_id), response=Response()))

            asyncio.run(call(2))
            self.assertEqual(sent[-1], ("このコマンドは登録されたユーザーだけが使えます。", True))
            asyncio.run(call(1))
            text, ephemeral = sent[-1]
            self.assertTrue(ephemeral)
            self.assertIn(secrets_box.public_key_b64(secrets_box.load_or_create_key(Path(td))), text)


if __name__ == "__main__":
    unittest.main()
