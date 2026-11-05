"""Feedback → a private GitHub repository, so the owner need not export it by hand (language-learning-audio's feedback handoff).

After each feedback, help request or new lesson record, the bot commits what changed to ``FEEDBACK_SYNC_REPO`` through the GitHub API (no git clone on
the machine). The token is a sealed secret (``FEEDBACK_SYNC_TOKEN``, ``src/secrets_box.py``) with write access to that one repository only.

What goes in, per learner, under a pseudonym (``learner-<8 hex>``, an HMAC of the name with a salt kept on the machine), one folder per lesson record:

    learner-1a2b3c4d/lesson-045/
      feedback.jsonl            the feedback lines for that record, without the learner's name
      support_requests.jsonl    the help requests for that record, without the learner's name
      manifest.json, lesson-045.plan.json, ….transcript.md, learner.before.json, …
                                the whole record (to replay the lesson), for the names in FEEDBACK_SYNC_FULL only

Other learners' folders hold only the two .jsonl files (their notes and form answers), and only when FEEDBACK_SYNC_OTHERS is on. Server paths are
replaced with ``<root>`` in every file. The trip profile never reaches a record (the record keeps its sha256 only, ``feedback.Ledger.save_manifest``).

A failed sync changes nothing on the machine: what was not pushed is pushed by the next one (the state file keeps, per folder, the digest pushed last).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import feedback

API = "https://api.github.com"
STATE_FILE = ".feedback_sync.json"  # under LESSON_ROOT, outside git
SALT_FILE = ".feedback_sync_salt"  # under LESSON_ROOT, mode 600: the pseudonyms can't be reversed without it
BATCH_BYTES = 4 << 20  # one commit carries at most about this much (the first sync of a long course is split)
DELAY_S = 10.0  # a kick waits this long, so the files written right after a record (review_backlog.json) ride the same commit
NOTES_FILES = ("feedback.jsonl", "support_requests.jsonl")


@dataclass
class SyncConfig:
    repo: str  # owner/name
    root: Path  # LESSON_ROOT
    users: list[str]  # learner names
    full: set[str] = field(default_factory=set)  # names whose whole records are synced
    others: bool = True  # sync the notes and form answers of the other names
    branch: str = "main"

    @classmethod
    def from_module(cls, config: Any, root: Path, users: list[str]) -> "SyncConfig | None":
        repo = str(getattr(config, "FEEDBACK_SYNC_REPO", "") or "").strip()
        if not repo:
            return None
        return cls(
            repo=repo,
            root=Path(root),
            users=list(users),
            full=set(getattr(config, "FEEDBACK_SYNC_FULL", []) or []),
            others=bool(getattr(config, "FEEDBACK_SYNC_OTHERS", True)),
            branch=str(getattr(config, "FEEDBACK_SYNC_BRANCH", "main") or "main"),
        )


def load_salt(root: Path) -> bytes:
    path = Path(root) / SALT_FILE
    try:
        salt = bytes.fromhex(path.read_text("ascii").strip())
        if salt:
            return salt
    except (OSError, ValueError):
        pass
    salt = secrets.token_bytes(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(salt.hex())
    return salt


def pseudonym(name: str, salt: bytes) -> str:
    return "learner-" + hmac.new(salt, name.encode("utf-8"), hashlib.sha256).hexdigest()[:8]


def _jsonl(lines: list[dict]) -> bytes:
    return "".join(json.dumps({k: v for k, v in line.items() if k != "user"}, ensure_ascii=False) + "\n" for line in lines).encode("utf-8")


def _support_lines(user_dir: Path) -> list[dict]:
    path = user_dir / feedback.SUPPORT_FILE
    if not path.exists():
        return []
    out = []
    for raw in path.read_bytes().split(b"\n"):
        try:
            line = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(line, dict):
            out.append(line)
    return out


# A path starts here: not inside a word, a number or another path («go out.», «1.0», «lesson-045.2» stay as they are)
_PATH_START = r"(?<![\w.~/-])"


def _path_pattern(path: str) -> re.Pattern:
    """``path`` where it begins a path: followed by «/», or, for an absolute path (never prose), also at the end of a value."""
    end = r"(?=/)" if not path.startswith("/") else r"(?=[/\"'\s]|$)"
    return re.compile(_PATH_START + re.escape(path) + end, re.M)


def scrub(data: bytes, root: Path, names: dict[str, str] | None = None) -> bytes:
    """Server paths out: LESSON_ROOT (and the bot's own directory) become ``<root>`` / ``<bot>``, and a learner's directory under it takes the
    learner's pseudonym (``names``: name → pseudonym), so ``<root>/<name>/learner.json`` in a record's arguments names no one. Only where a path
    begins: a relative root («out», «.») is not replaced inside words or numbers. With the root «.», a learner's directory is written without it
    («neko/learner.json») and is matched by the name alone. Binary files are left as they are."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    root = Path(root)
    bot_dir = str(Path(__file__).resolve().parent.parent)
    for path, mark in ((str(root.resolve()), "<root>"), (str(root), "<root>"), (bot_dir, "<bot>")):
        if path and path != "/":
            text = _path_pattern(path).sub(mark, text)
    bare = root == Path(".")
    for name, who in (names or {}).items():
        text = re.sub(r"<root>/" + re.escape(name) + r"(?=[/\"\s]|$)", "<root>/" + who, text)
        if bare:
            text = re.sub(_PATH_START + re.escape(name) + r"(?=/)", "<root>/" + who, text)
    return text.encode("utf-8")


def folders(cfg: SyncConfig, salt: bytes) -> dict[str, dict[str, bytes]]:
    """What the repository should hold: folder (``learner-…/lesson-045``) → file name → content."""
    out: dict[str, dict[str, bytes]] = {}
    names = {name: pseudonym(name, salt) for name in cfg.users}
    for name in cfg.users:
        full = name in cfg.full
        if not full and not cfg.others:
            continue
        ledger = feedback.Ledger(cfg.root / name)
        events: dict[str, list[dict]] = {}
        for e in ledger.events():
            if isinstance(e.get("manifest"), str):
                events.setdefault(e["manifest"], []).append(e)
        support: dict[str, list[dict]] = {}
        for line in _support_lines(ledger.user_dir):
            if isinstance(line.get("manifest"), str):
                support.setdefault(line["manifest"], []).append(line)
        records = set(events) | set(support)
        dirs = {d.name: d for _, d in ledger._dirs()}
        if full:
            records |= set(dirs)
        who = pseudonym(name, salt)
        for record in sorted(records):
            if feedback.MANIFEST_RE.match(record) is None:
                continue  # only a record's own id names a folder
            files: dict[str, bytes] = {}
            if record in events:
                files["feedback.jsonl"] = _jsonl(events[record])
            if record in support:
                files["support_requests.jsonl"] = _jsonl(support[record])
            if full and record in dirs:
                for p in sorted(dirs[record].iterdir()):
                    if p.is_file() and p.name not in NOTES_FILES:
                        files[p.name] = p.read_bytes()
            out[f"{who}/{record}"] = {k: scrub(v, cfg.root, names) for k, v in files.items()}
    return out


def digest(files: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for k in sorted(files):
        h.update(k.encode("utf-8") + b"\0" + hashlib.sha256(files[k]).digest())
    return h.hexdigest()


def batches(changed: dict[str, dict[str, bytes]], limit: int = BATCH_BYTES) -> list[list[str]]:
    """Folders grouped into commits of about ``limit`` bytes (a folder is never split)."""
    out: list[list[str]] = []
    size = 0
    for folder in sorted(changed):
        n = sum(len(v) for v in changed[folder].values())
        if not out or (size + n > limit and out[-1]):
            out.append([])
            size = 0
        out[-1].append(folder)
        size += n
    return out


# (method, path, json body) → (status, json reply)
Request = Callable[[str, str, dict | None], Awaitable[tuple[int, Any]]]


class GitHubError(RuntimeError):
    pass


async def commit(request: Request, repo: str, branch: str, files: dict[str, bytes], message: str) -> str:
    """One commit on ``branch`` that adds or replaces ``files`` (path → content), keeping the rest of the tree. Returns the commit's sha.
    A ref moved meanwhile (another push) is retried once from the new head."""
    for attempt in range(2):
        status, ref = await request("GET", f"/repos/{repo}/git/ref/heads/{branch}", None)
        if status != 200:
            raise GitHubError(f"reading {branch} failed (HTTP {status})")
        head = ref["object"]["sha"]
        status, parent = await request("GET", f"/repos/{repo}/git/commits/{head}", None)
        if status != 200:
            raise GitHubError(f"reading the head commit failed (HTTP {status})")
        entries = []
        for path, data in sorted(files.items()):
            try:
                entries.append({"path": path, "mode": "100644", "type": "blob", "content": data.decode("utf-8")})
            except UnicodeDecodeError:
                status, blob = await request("POST", f"/repos/{repo}/git/blobs", {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"})
                if status != 201:
                    raise GitHubError(f"creating a blob failed (HTTP {status})")
                entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        status, tree = await request("POST", f"/repos/{repo}/git/trees", {"base_tree": parent["tree"]["sha"], "tree": entries})
        if status != 201:
            raise GitHubError(f"creating the tree failed (HTTP {status})")
        status, made = await request("POST", f"/repos/{repo}/git/commits", {"message": message, "tree": tree["sha"], "parents": [head]})
        if status != 201:
            raise GitHubError(f"creating the commit failed (HTTP {status})")
        status, _ = await request("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", {"sha": made["sha"]})
        if status == 200:
            return made["sha"]
        if status != 422 or attempt:
            raise GitHubError(f"moving {branch} failed (HTTP {status})")
    raise GitHubError("unreachable")


def aiohttp_request(token: str) -> Request:
    """The GitHub REST API with the token in the header only (never in a URL, a log line or an error)."""
    import aiohttp

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "site-update-notifier-feedback-sync"}  # fmt: skip

    async def request(method: str, path: str, body: dict | None) -> tuple[int, Any]:
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as session:
            async with session.request(method, API + path, json=body) as resp:
                try:
                    data = await resp.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    data = None
                return resp.status, data

    return request


class FeedbackSync:
    def __init__(self, cfg: SyncConfig, request: Request, delay: float = DELAY_S) -> None:
        self.cfg = cfg
        self.request = request
        self.delay = delay
        self.state_path = cfg.root / STATE_FILE
        self._task: asyncio.Task | None = None
        self._again = False

    @property
    def _key(self) -> str:
        """Progress is kept per repository and branch (review of #197): a new destination branch gets every folder, not only what changed."""
        return f"{self.cfg.repo}@{self.cfg.branch}"

    def _read(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _state(self) -> dict[str, str]:
        data = self._read()
        pushed = data.get(self._key)
        if pushed is None and self.cfg.branch == "main" and isinstance(data.get(self.cfg.repo), dict):
            pushed = data[self.cfg.repo]  # the state before branches were kept: every sync then went to the default branch, «main»
            self._save_state(dict(pushed))  # migrated now, whether or not anything is pushed
        return dict(pushed) if isinstance(pushed, dict) else {}

    def _save_state(self, pushed: dict[str, str]) -> None:
        data = self._read()
        data[self._key] = pushed
        if self.cfg.branch == "main":
            data.pop(self.cfg.repo, None)  # migrated: the old key no longer stands for anything
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), "utf-8")
        os.replace(tmp, self.state_path)

    async def run(self) -> int:
        """Push every folder whose content differs from what was pushed last. Returns the number of folders pushed."""
        want = folders(self.cfg, load_salt(self.cfg.root))
        pushed = self._state()
        digests = {k: digest(v) for k, v in want.items()}
        changed = {k: v for k, v in want.items() if pushed.get(k) != digests[k]}
        done = 0
        for group in batches(changed):
            files = {f"{folder}/{name}": data for folder in group for name, data in changed[folder].items()}
            await commit(self.request, self.cfg.repo, self.cfg.branch, files, "Feedback: " + ", ".join(group))
            for folder in group:
                pushed[folder] = digests[folder]
            self._save_state(pushed)
            done += len(group)
        return done

    def kick(self) -> None:
        """Sync soon, in the background. Never raises: a failure is logged (without the token) and retried by the next kick."""
        if self._task is not None and not self._task.done():
            self._again = True
            return
        try:
            self._task = asyncio.get_running_loop().create_task(self._loop())
        except RuntimeError:  # no running loop (a test calling a handler directly)
            self._task = None

    async def _loop(self) -> None:
        while True:
            self._again = False
            await asyncio.sleep(self.delay)
            try:
                n = await self.run()
                if n:
                    logging.info("フィードバックを %s に同期しました（%d 件）", self.cfg.repo, n)
            except Exception as e:  # noqa: BLE001  network, API, disk: the next kick retries
                logging.warning("フィードバックの同期に失敗しました (%s: %s)", type(e).__name__, e)
            if not self._again:
                return


def setup(config: Any, root: Path, users: list[str], token: str | None) -> FeedbackSync | None:
    cfg = SyncConfig.from_module(config, root, users)
    if cfg is None:
        return None
    if not token:
        logging.warning("FEEDBACK_SYNC_REPO があるが、封じた secrets に FEEDBACK_SYNC_TOKEN がないので、フィードバックは同期しません")
        return None
    return FeedbackSync(cfg, aiohttp_request(token))
