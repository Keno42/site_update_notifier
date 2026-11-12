import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src import feedback, feedback_sync, secrets_box


class FakeGitHub:
    """The git data API over an in-memory repository: path → text."""

    def __init__(self, fail_patch: int = 0) -> None:
        self.commits = {"c0": {"tree": "t0", "files": {"README.md": "x"}}}
        self.trees = {"t0": {"README.md": "x"}}
        self.head = "c0"
        self.messages: list[str] = []
        self.fail_patch = fail_patch
        self.calls: list[tuple[str, str]] = []
        self.down = False

    @property
    def files(self) -> dict[str, str]:
        return self.trees[self.commits[self.head]["tree"]]

    async def __call__(self, method, path, body):
        self.calls.append((method, path))
        if self.down:
            return 503, None
        if method == "GET" and "/git/ref/heads/" in path:
            return 200, {"object": {"sha": self.head}}
        if method == "GET" and "/git/commits/" in path:
            sha = path.rsplit("/", 1)[1]
            return 200, {"tree": {"sha": self.commits[sha]["tree"]}}
        if method == "POST" and path.endswith("/git/trees"):
            tree = dict(self.trees[body["base_tree"]])
            for e in body["tree"]:
                tree[e["path"]] = e["content"]
            sha = f"t{len(self.trees)}"
            self.trees[sha] = tree
            return 201, {"sha": sha}
        if method == "POST" and path.endswith("/git/commits"):
            sha = f"c{len(self.commits)}"
            self.commits[sha] = {"tree": body["tree"], "parent": body["parents"][0]}
            self.messages.append(body["message"])
            return 201, {"sha": sha}
        if method == "PATCH":
            if self.fail_patch:
                self.fail_patch -= 1
                return 422, None
            self.head = body["sha"]
            return 200, {}
        return 404, None


def write_record(user_dir: Path, lesson: int, args: list[str]) -> str:
    d = user_dir / feedback.MANIFESTS / f"lesson-{lesson:03d}"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"id": d.name, "lesson": lesson, "generate_args": args, "trip_sha256": "abc"}), "utf-8")
    (d / f"lesson-{lesson:03d}.transcript.md").write_text("Góðan daginn.", "utf-8")
    (d / feedback.LEARNER_BEFORE).write_text("{}", "utf-8")
    return d.name


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = Path(self.td.name)
        trip = str(self.root / "owner" / "trip.toml")
        learner = str(self.root / "owner" / "learner.json")
        self.owner_record = write_record(self.root / "owner", 45, ["generate", "--learner", learner, "--out", str(self.root / "owner" / "work"), "--trip", trip])
        feedback.Ledger(self.root / "owner").append({"user": "owner", "manifest": self.owner_record, "note": "the café scene was odd"})
        other = write_record(self.root / "friend", 3, ["generate"])
        feedback.Ledger(self.root / "friend").append({"user": "friend", "manifest": other, "note": "the app froze once", "load": "right"})
        with (self.root / "friend" / feedback.SUPPORT_FILE).open("w", encoding="utf-8") as f:
            f.write(json.dumps({"user": "friend", "manifest": other, "requests": [{"kind": "grammar", "item_id": "x", "question": "why?"}]}) + "\n")
        write_record(self.root / "friend", 4, ["generate"])  # no feedback for this one
        self.cfg = feedback_sync.SyncConfig(repo="o/r", root=self.root, users=["owner", "friend"], full={"owner"})
        self.salt = feedback_sync.load_salt(self.root)
        self.owner = feedback_sync.pseudonym("owner", self.salt)
        self.friend = feedback_sync.pseudonym("friend", self.salt)

    def sync(self, gh: FakeGitHub) -> int:
        return asyncio.run(feedback_sync.FeedbackSync(self.cfg, gh, delay=0).run())

    def test_the_owner_gets_the_whole_record_and_others_only_their_notes_under_pseudonyms(self):
        gh = FakeGitHub()
        self.assertEqual(self.sync(gh), 2)
        files = gh.files
        own = f"{self.owner}/lesson-045/"
        self.assertIn(own + "manifest.json", files)
        self.assertIn(own + "learner.before.json", files)
        self.assertIn(own + "lesson-045.transcript.md", files)
        self.assertIn("the café scene was odd", files[own + "feedback.jsonl"])
        theirs = sorted(p for p in files if p.startswith(self.friend))
        self.assertEqual(theirs, [f"{self.friend}/lesson-003/feedback.jsonl", f"{self.friend}/lesson-003/support_requests.jsonl"])
        self.assertIn("the app froze once", files[theirs[0]])
        everything = "\n".join(list(files) + list(files.values()))
        for secret in ("owner", "friend", str(self.root)):
            self.assertNotIn(f'"{secret}"' if secret in ("owner", "friend") else secret, everything)
        self.assertNotIn(self.td.name, everything, "no server path")
        manifest = files[own + "manifest.json"]
        self.assertIn(f"<root>/{self.owner}/trip.toml", manifest, "the learner's directory takes the pseudonym")
        for name in ("owner", "friend"):
            for path, text in files.items():
                self.assertNotRegex(path + "\n" + text, rf"\b{name}\b", f"{name} in {path}")

    def test_the_pseudonym_is_stable_and_not_the_name(self):
        self.assertEqual(self.owner, feedback_sync.pseudonym("owner", feedback_sync.load_salt(self.root)))
        self.assertNotIn("owner", self.owner)
        self.assertRegex(self.owner, r"^learner-[0-9a-f]{8}$")
        self.assertNotEqual(self.owner, feedback_sync.pseudonym("owner", b"another salt"))

    def test_only_what_changed_is_pushed_again(self):
        gh = FakeGitHub()
        self.sync(gh)
        self.assertEqual(self.sync(gh), 0)
        self.assertEqual(len(gh.messages), 1)
        feedback.Ledger(self.root / "friend").append({"user": "friend", "manifest": "lesson-004", "note": "too long"})
        self.assertEqual(self.sync(gh), 1)
        self.assertIn("lesson-004", gh.messages[-1])
        self.assertIn(f"{self.owner}/lesson-045/manifest.json", gh.files, "the earlier files stay")

    def test_a_sync_reads_only_the_records_that_changed_and_never_on_the_event_loop(self):
        """Lesson 54: every sync read and scrubbed every record on the event loop, so the bot answered nothing for many seconds: a help request
        started a sync, the send button pressed meanwhile got «interaction failed», and its five presses were all recorded when the loop came back."""
        import threading
        from unittest import mock

        gh = FakeGitHub()
        sync = feedback_sync.FeedbackSync(self.cfg, gh, delay=0)
        loop_thread = threading.get_ident()
        read: list[tuple[str, bool]] = []
        real = feedback_sync.scrub

        def scrub(data, root, names=None):
            read.append((data[:12].decode("utf-8", "replace"), threading.get_ident() == loop_thread))
            return real(data, root, names)

        with mock.patch.object(feedback_sync, "scrub", scrub):
            self.assertEqual(asyncio.run(sync.run()), 2)
            self.assertTrue(read)
            self.assertFalse(any(on_loop for _, on_loop in read), "the records are read in a worker thread")
            first = len(read)
            read.clear()
            self.assertEqual(asyncio.run(sync.run()), 0)
            self.assertLess(len(read), first)
            self.assertFalse(any(head.startswith(("Góðan", '{"id"')) for head, _ in read), "no record file is read again")
            (self.root / "owner" / feedback.MANIFESTS / "lesson-045" / "lesson-045.transcript.md").write_text("Gott kvöld, allir saman.", "utf-8")
            read.clear()
            self.assertEqual(asyncio.run(sync.run()), 1)
            self.assertIn("Gott kvöld", gh.files[f"{self.owner}/lesson-045/lesson-045.transcript.md"])

    def test_the_digests_are_those_of_the_contents_so_an_earlier_state_still_holds(self):
        want = feedback_sync.folders(self.cfg, self.salt)
        self.assertEqual(feedback_sync.Digests().of(self.cfg, self.salt), {k: feedback_sync.digest(v) for k, v in want.items()})
        only = feedback_sync.folders(self.cfg, self.salt, {f"{self.owner}/lesson-045"})
        self.assertEqual(only, {f"{self.owner}/lesson-045": want[f"{self.owner}/lesson-045"]})

    def test_one_feedback_stored_several_times_is_exported_once(self):
        """Lesson 54's export held the same feedback five times: the send button was pressed again while the bot did not answer."""
        ledger = feedback.Ledger(self.root / "owner")
        same = {"user": "owner", "manifest": self.owner_record, "load": "light", "note": "too thick"}
        for second in (7, 7, 8):
            ledger.append({**same, "ts": f"2026-11-12T05:58:0{second}+09:00"})
        ledger.append({**same, "ts": "2026-11-12T06:10:00+09:00", "note": "another thought"})
        gh = FakeGitHub()
        self.sync(gh)
        lines = [json.loads(x) for x in gh.files[f"{self.owner}/lesson-045/feedback.jsonl"].splitlines()]
        self.assertEqual([x.get("note") for x in lines], ["the café scene was odd", "too thick", "another thought"])
        self.assertEqual(len(ledger.events(self.owner_record)), 5, "the machine's own file is append-only and keeps every line")

    def test_progress_is_kept_per_branch(self):
        """Review of #197: after a change of `FEEDBACK_SYNC_BRANCH` the new branch got nothing (the progress was kept per repository)."""
        gh = FakeGitHub()
        self.assertEqual(self.sync(gh), 2)
        self.cfg.branch = "other"
        self.assertEqual(self.sync(gh), 2, "every folder again, for the new branch")
        self.assertEqual(self.sync(gh), 0)
        self.cfg.branch = "main"
        self.assertEqual(self.sync(gh), 0, "the first branch's progress is kept")

    def test_the_state_before_branches_counts_for_main_only(self):
        gh = FakeGitHub()
        self.sync(gh)
        path = self.root / feedback_sync.STATE_FILE
        data = json.loads(path.read_text("utf-8"))
        path.write_text(json.dumps({"o/r": data["o/r@main"]}), "utf-8")  # as #197 wrote it
        self.assertEqual(self.sync(gh), 0, "the old progress is main's")
        self.assertEqual(set(json.loads(path.read_text("utf-8"))), {"o/r@main"}, "migrated on the next save")
        path.write_text(json.dumps({"o/r": data["o/r@main"]}), "utf-8")
        self.cfg.branch = "other"
        self.assertEqual(self.sync(gh), 2, "another branch starts from nothing")

    def test_a_failure_pushes_nothing_and_the_next_sync_catches_up(self):
        gh = FakeGitHub()
        gh.down = True
        with self.assertRaises(feedback_sync.GitHubError):
            self.sync(gh)
        gh.down = False
        self.assertEqual(self.sync(gh), 2)

    def test_a_moved_branch_is_retried_once_from_the_new_head(self):
        gh = FakeGitHub(fail_patch=1)
        self.assertEqual(self.sync(gh), 2)
        (self.root / feedback_sync.STATE_FILE).unlink()  # a fresh repository
        gh = FakeGitHub(fail_patch=2)
        with self.assertRaises(feedback_sync.GitHubError):
            self.sync(gh)

    def test_others_off_syncs_the_full_names_only(self):
        self.cfg.others = False
        gh = FakeGitHub()
        self.sync(gh)
        self.assertFalse([p for p in gh.files if p.startswith(self.friend)])

    def test_large_syncs_are_split_into_commits_without_splitting_a_folder(self):
        changed = {"a/1": {"x": b"1" * 6}, "a/2": {"x": b"1" * 6}, "a/3": {"x": b"1" * 3}}
        self.assertEqual(feedback_sync.batches(changed, limit=10), [["a/1"], ["a/2", "a/3"]])

    def test_kick_runs_in_the_background_and_logs_a_failure_without_raising(self):
        gh = FakeGitHub()
        gh.down = True
        sync = feedback_sync.FeedbackSync(self.cfg, gh, delay=0)

        async def go():
            sync.kick()
            sync.kick()  # a second kick while running reruns once, after
            await sync._task

        with self.assertLogs(level="WARNING") as logs:
            asyncio.run(go())
        self.assertIn("HTTP 503", logs.output[0])


class ScrubTests(unittest.TestCase):
    """Review of #197: a relative LESSON_ROOT was replaced everywhere («go out.» → «go <root>.», «1.0» → «1<root>0», invalid JSON)."""

    NAMES = {"neko": "learner-x"}

    def scrub(self, text: str, root: str) -> str:
        return feedback_sync.scrub(text.encode(), Path(root), self.NAMES).decode()

    def test_a_relative_root_is_replaced_only_where_a_path_begins(self):
        self.assertEqual(self.scrub('Say you are about to go out. "out/neko/learner.json" outlet/x', "out"),
                         'Say you are about to go out. "<root>/learner-x/learner.json" outlet/x')

    def test_the_root_dot_leaves_numbers_and_ids_and_still_hides_the_learner(self):
        text = json.dumps({"id": "lesson-045.2", "rate": 1.0, "args": ["--learner", "neko/learner.json", "--out", "./neko/work", "../x"]})
        out = self.scrub(text, ".")
        data = json.loads(out)  # still valid JSON
        self.assertEqual(data["id"], "lesson-045.2")
        self.assertEqual(data["rate"], 1.0)
        self.assertEqual(data["args"], ["--learner", "<root>/learner-x/learner.json", "--out", "<root>/learner-x/work", "../x"])

    def test_an_absolute_root_is_matched_whole_not_as_a_prefix_of_another_path(self):
        self.assertEqual(self.scrub('"/srv/l/neko/x" "/srv/lessons/y" "/srv/l"', "/srv/l"), '"<root>/learner-x/x" "/srv/lessons/y" "<root>"')


class SetupTests(unittest.TestCase):
    def test_without_a_repo_or_a_token_nothing_syncs(self):
        root = Path(".")
        self.assertIsNone(feedback_sync.setup(SimpleNamespace(), root, ["a"], "token"))
        with self.assertLogs(level="WARNING"):
            self.assertIsNone(feedback_sync.setup(SimpleNamespace(FEEDBACK_SYNC_REPO="o/r"), root, ["a"], None))
        sync = feedback_sync.setup(SimpleNamespace(FEEDBACK_SYNC_REPO="o/r", FEEDBACK_SYNC_FULL=["a"]), root, ["a", "b"], "token")
        self.assertEqual(sync.cfg.full, {"a"})
        self.assertTrue(sync.cfg.others)

    def test_the_token_never_reaches_the_lesson_subprocess(self):
        secrets_box.SECRETS.clear()
        secrets_box.SECRETS.update({"AZURE_SPEECH_KEY": "k", "AZURE_SPEECH_REGION": "r", "FEEDBACK_SYNC_TOKEN": "ghp_secret"})
        self.addCleanup(secrets_box.SECRETS.clear)
        self.assertEqual(secrets_box.environment(), {"AZURE_SPEECH_KEY": "k", "AZURE_SPEECH_REGION": "r"})
        self.assertEqual(secrets_box.status(), "secrets: azure ✓ / feedback-sync ✓")


if __name__ == "__main__":
    unittest.main()
