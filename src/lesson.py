"""/lesson: 前回レッスンの振り返り → report → 次のレッスン生成 → 投稿.

language-learning-audio (submodule: external/language-learning-audio) の CLI を
subprocess で呼ぶ。永続化するのはユーザーごとの learner.json と、次の振り返りに
使う pending_review.json だけで、生成物は投稿後に消す。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import discord
from discord import app_commands

LLA_DIR = (
    Path(__file__).resolve().parent.parent / "external" / "language-learning-audio"
)
RESULTS = {"ok": "言えた", "shaky": "迷った", "failed": "言えなかった"}


@dataclass
class LessonConfig:
    root: Path
    users: dict[int, str]
    channel_id: int = 0
    guild_id: int = 0
    curriculum: str = "curricula/is-en"
    known: str = "ja"
    profile: str = "profiles/edge-is-ja.toml"
    minutes: float = 30
    extra_args: list[str] = field(default_factory=list)
    keep_cache: bool = False
    upload_limit_mb: float = 20
    review_limit: int = 0
    python: str = sys.executable
    lla_dir: Path = LLA_DIR

    @classmethod
    def from_module(cls, config: Any) -> "LessonConfig | None":
        """config.py の LESSON_* から作る。LESSON_ROOT と LESSON_USERS がなければ無効."""
        root = getattr(config, "LESSON_ROOT", "")
        users = getattr(config, "LESSON_USERS", {})
        if not root or not users:
            return None
        defaults = cls(root=Path(root), users=dict(users))
        return cls(
            root=Path(root),
            users={int(k): str(v) for k, v in users.items()},
            channel_id=getattr(config, "LESSON_CHANNEL_ID", defaults.channel_id),
            guild_id=getattr(config, "LESSON_GUILD_ID", defaults.guild_id),
            curriculum=getattr(config, "LESSON_CURRICULUM", defaults.curriculum),
            known=getattr(config, "LESSON_KNOWN", defaults.known),
            profile=getattr(config, "LESSON_PROFILE", defaults.profile),
            minutes=getattr(config, "LESSON_MINUTES", defaults.minutes),
            extra_args=list(getattr(config, "LESSON_EXTRA_ARGS", [])),
            keep_cache=getattr(config, "LESSON_KEEP_CACHE", defaults.keep_cache),
            upload_limit_mb=getattr(
                config, "LESSON_UPLOAD_LIMIT_MB", defaults.upload_limit_mb
            ),
            review_limit=getattr(config, "LESSON_REVIEW_LIMIT", defaults.review_limit),
        )

    def user_dir(self, name: str) -> Path:
        if not name or "/" in name or name in (".", ".."):
            raise ValueError(f"learner name must be a plain name: {name!r}")
        return self.root / name

    def learner_path(self, name: str) -> Path:
        return self.user_dir(name) / "learner.json"

    def pending_path(self, name: str) -> Path:
        return self.user_dir(name) / "pending_review.json"

    def work_dir(self, name: str) -> Path:
        return self.user_dir(name) / "work"

    def generate_args(self, name: str) -> list[str]:
        return [
            "generate",
            "--curriculum",
            self.curriculum,
            "--known",
            self.known,
            "--profile",
            self.profile,
            "--minutes",
            f"{self.minutes:g}",
            "--learner",
            str(self.learner_path(name)),
            "--out",
            str(self.work_dir(name)),
            *self.extra_args,
        ]

    def report_args(self, name: str, lesson: int, failed: list[str]) -> list[str]:
        args = ["report", "--learner", str(self.learner_path(name))]
        args += ["--lesson", str(lesson)]
        if failed:
            args += ["--failed", ",".join(failed)]
        return args


# ---------------------------------------------------------------- review data


def pending_from_plan(plan: dict, limit: int = 0) -> dict:
    """plan.json の review から次回の振り返りを作る。limit > 0 なら新出項目を含む問いを優先."""
    questions = [q for q in plan.get("review", []) if q.get("items")]
    if limit > 0 and len(questions) > limit:
        new = {i["id"] for i in plan.get("new_items", [])}
        order = sorted(
            range(len(questions)),
            key=lambda n: (not new & set(questions[n]["items"]), n),
        )
        questions = [questions[n] for n in sorted(order[:limit])]
    return {"lesson": plan["lesson_number"], "questions": questions}


def save_pending(path: Path, pending: dict) -> None:
    if pending["questions"]:
        path.write_text(json.dumps(pending, ensure_ascii=False, indent=1), "utf-8")
    else:
        path.unlink(missing_ok=True)


def load_pending(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        pending = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as e:
        logging.error(f"振り返りデータを読めませんでした: {path}: {e}")
        return None
    return pending if pending.get("questions") else None


@dataclass
class ReviewSession:
    lesson: int
    questions: list[dict]
    results: list[str] = field(default_factory=list)

    @property
    def done(self) -> bool:
        return len(self.results) >= len(self.questions)

    @property
    def current(self) -> dict:
        return self.questions[len(self.results)]

    def rate(self, result: str) -> None:
        if result not in RESULTS:
            raise ValueError(result)
        if not self.done:
            self.results.append(result)

    def _ids(self, result: str) -> list[str]:
        ids: list[str] = []
        for q, r in zip(self.questions, self.results):
            if r == result:
                ids += [i for i in q["items"] if i not in ids]
        return ids

    def failed_ids(self) -> list[str]:
        return self._ids("failed")

    def shaky_ids(self) -> list[str]:
        return self._ids("shaky")

    def render(self) -> str:
        q = self.current
        return (
            f"**振り返り {len(self.results) + 1}/{len(self.questions)}**"
            f"（レッスン{self.lesson}）\n{q['prompt']}\n答え: ||{q['answer']}||"
        )

    def summary(self) -> str:
        answers = {i: q["answer"] for q in self.questions for i in q["items"]}
        lines = [f"**振り返り完了**（レッスン{self.lesson}、{len(self.results)}問）"]
        for result in ("failed", "shaky"):
            ids = self._ids(result)
            if ids:
                shown = "、".join(dict.fromkeys(answers[i] for i in ids))
                lines.append(f"{RESULTS[result]}: {shown}")
        if self.shaky_ids():
            lines.append("（迷った項目は今のところ言えた扱いです）")
        return "\n".join(lines)


# ---------------------------------------------------------------- files


def latest_plan(work: Path) -> Path | None:
    plans = sorted(work.glob("lesson-*.plan.json"))
    return plans[-1] if plans else None


def cleanup(work: Path, keep_cache: bool) -> None:
    """生成物を消す。keep_cache なら TTS キャッシュ (work/cache) だけ残す."""
    if not work.exists():
        return
    for p in work.iterdir():
        if p.name == "cache" and keep_cache:
            continue
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()


async def fit_upload(audio: Path, limit_bytes: int) -> Path | None:
    """上限を超えるなら ffmpeg でビットレートを落とす。収まらなければ None."""
    if audio.stat().st_size <= limit_bytes:
        return audio
    for bitrate in ("32k", "24k", "16k"):
        out = audio.with_name(f"{audio.stem}-{bitrate}.mp3")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(audio),
            "-ac", "1", "-codec:a", "libmp3lame", "-b:a", bitrate, str(out),
        )  # fmt: skip
        if await proc.wait() != 0:
            return None
        if out.stat().st_size <= limit_bytes:
            return out
    return None


async def run_cli(cfg: LessonConfig, args: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        cfg.python,
        "-m",
        "audiolesson.cli",
        *args,
        cwd=str(cfg.lla_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return (
        proc.returncode or 0,
        out.decode(errors="replace"),
        err.decode(errors="replace"),
    )


def _tail(text: str, limit: int = 1500) -> str:
    return text[-limit:] if len(text) > limit else text


# ---------------------------------------------------------------- flow


class Lessons:
    def __init__(self, cfg: LessonConfig) -> None:
        self.cfg = cfg
        self.busy: set[str] = set()

    async def start(self, interaction: discord.Interaction) -> None:
        name = self.cfg.users.get(interaction.user.id)
        if name is None:
            await interaction.response.send_message(
                "このコマンドは登録されたユーザーだけが使えます。", ephemeral=True
            )
            return
        if self.cfg.channel_id and interaction.channel_id != self.cfg.channel_id:
            await interaction.response.send_message(
                f"<#{self.cfg.channel_id}> で実行してください。", ephemeral=True
            )
            return
        if name in self.busy:
            await interaction.response.send_message(
                "前の /lesson がまだ進行中です。", ephemeral=True
            )
            return
        channel = interaction.channel
        if not isinstance(channel, discord.abc.Messageable):
            await interaction.response.send_message(
                "このチャンネルには投稿できません。", ephemeral=True
            )
            return
        self.busy.add(name)
        handed_to_view = False
        try:
            self.cfg.user_dir(name).mkdir(parents=True, exist_ok=True)
            pending = load_pending(self.cfg.pending_path(name))
            if pending is None:
                await interaction.response.send_message("レッスンを生成しています…")
                await self.guarded(channel, self.generate_and_post(channel, name))
                return
            session = ReviewSession(pending["lesson"], pending["questions"])

            async def finish(reviewed: bool) -> None:
                async def run() -> None:
                    if reviewed and not await self.report(channel, name, session):
                        return
                    await self.generate_and_post(channel, name)

                try:
                    await self.guarded(channel, run())
                finally:
                    self.busy.discard(name)

            async def expire() -> None:
                self.busy.discard(name)

            view = ReviewView(session, interaction.user.id, finish, expire)
            await interaction.response.send_message(session.render(), view=view)
            handed_to_view = True
            view.message = await interaction.original_response()
        finally:
            if not handed_to_view:
                self.busy.discard(name)

    @staticmethod
    async def guarded(channel: discord.abc.Messageable, work: Awaitable[None]) -> None:
        """想定外の例外もチャンネルに知らせる (ボタンのコールバックでは握りつぶされるため)."""
        try:
            await work
        except Exception as e:
            logging.exception("/lesson の処理中にエラーが発生しました")
            await channel.send(f"エラーが発生しました: {e}")

    async def report(
        self, channel: discord.abc.Messageable, name: str, session: ReviewSession
    ) -> bool:
        failed = session.failed_ids()
        rc, out, err = await run_cli(
            self.cfg, self.cfg.report_args(name, session.lesson, failed)
        )
        if rc != 0:
            await channel.send(f"report に失敗しました:\n```\n{_tail(err or out)}\n```")
            return False
        self.cfg.pending_path(name).unlink(missing_ok=True)
        return True

    async def generate_and_post(
        self, channel: discord.abc.Messageable, name: str
    ) -> None:
        work = self.cfg.work_dir(name)
        cleanup(work, self.cfg.keep_cache)
        work.mkdir(parents=True, exist_ok=True)
        try:
            async with channel.typing():
                rc, out, err = await run_cli(self.cfg, self.cfg.generate_args(name))
            if rc != 0:
                await channel.send(
                    f"生成に失敗しました:\n```\n{_tail(err or out)}\n```"
                )
                return
            plan_path = latest_plan(work)
            if plan_path is None:
                await channel.send("生成結果 (plan.json) が見つかりませんでした。")
                return
            plan = json.loads(plan_path.read_text("utf-8"))
            pending = pending_from_plan(plan, self.cfg.review_limit)
            save_pending(self.cfg.pending_path(name), pending)
            await self.post(channel, work, plan, bool(pending["questions"]))
        finally:
            cleanup(work, self.cfg.keep_cache)

    async def post(
        self,
        channel: discord.abc.Messageable,
        work: Path,
        plan: dict,
        has_review: bool,
    ) -> None:
        n = plan["lesson_number"]
        stem = work / f"lesson-{n:03d}"
        files = []
        audio_note = ""
        audio = next(
            (
                p
                for p in (stem.with_suffix(".mp3"), stem.with_suffix(".wav"))
                if p.exists()
            ),
            None,
        )
        if audio is not None:
            fitted = await fit_upload(
                audio, int(self.cfg.upload_limit_mb * 1024 * 1024)
            )
            if fitted is None:
                audio_note = (
                    "\n（音声がアップロード上限を超えたため添付できませんでした）"
                )
            else:
                name = f"lesson-{n:03d}{fitted.suffix}"
                files.append(discord.File(fitted, filename=name))
        transcript = stem.with_suffix(".transcript.md")
        if transcript.exists():
            files.append(discord.File(transcript))
        new = "、".join(i["target"] or i["id"] for i in plan.get("new_items", []))
        reviewed = len(plan.get("reviewed_items", []))
        minutes = plan.get("summary", {}).get("duration_s", 0) / 60
        text = (
            f"**レッスン {n}**（約{minutes:.0f}分）\n新出: {new or 'なし'}\n"
            f"復習: {reviewed}項目"
            + (
                "\n次回 /lesson の最初に、今回の振り返りをします。"
                if has_review
                else ""
            )
            + audio_note
        )
        try:
            await channel.send(text, files=files)
        finally:
            for f in files:
                f.close()


class ReviewView(discord.ui.View):
    """1問ずつ: 答えはスポイラー、評価ボタンを押すと次の問いに差し替え."""

    def __init__(
        self,
        session: ReviewSession,
        owner_id: int,
        finish: Callable[[bool], Awaitable[None]],
        expire: Callable[[], Awaitable[None]],
    ) -> None:
        super().__init__(timeout=1800)
        self.session = session
        self.owner_id = owner_id
        self.finish = finish
        self.expire = expire
        self.message: discord.InteractionMessage | None = None
        for result, style in (
            ("ok", discord.ButtonStyle.success),
            ("shaky", discord.ButtonStyle.secondary),
            ("failed", discord.ButtonStyle.danger),
        ):
            button: discord.ui.Button = discord.ui.Button(
                label=RESULTS[result], style=style
            )
            button.callback = self._rate_callback(result)  # type: ignore[method-assign]
            self.add_item(button)
        skip: discord.ui.Button = discord.ui.Button(
            label="振り返らずに生成", style=discord.ButtonStyle.secondary, row=1
        )
        skip.callback = self._skip  # type: ignore[method-assign]
        self.add_item(skip)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "本人だけが回答できます。", ephemeral=True
            )
            return False
        return True

    def _rate_callback(
        self, result: str
    ) -> Callable[[discord.Interaction], Awaitable[None]]:
        async def callback(interaction: discord.Interaction) -> None:
            if self.is_finished():
                return
            self.session.rate(result)
            if not self.session.done:
                await interaction.response.edit_message(content=self.session.render())
                return
            self.stop()
            await interaction.response.edit_message(
                content=self.session.summary() + "\n\n次のレッスンを生成しています…",
                view=None,
            )
            await self.finish(True)

        return callback

    async def _skip(self, interaction: discord.Interaction) -> None:
        if self.is_finished():
            return
        self.stop()
        await interaction.response.edit_message(
            content="振り返りをスキップしました。次のレッスンを生成しています…",
            view=None,
        )
        await self.finish(False)

    async def on_timeout(self) -> None:
        await self.expire()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="時間切れです。/lesson で振り返りを最初からやり直せます。",
                    view=None,
                )
            except discord.DiscordException:
                pass


def setup(client: discord.Client, config: Any) -> Callable[[], Awaitable[None]] | None:
    """config に LESSON_ROOT と LESSON_USERS があれば /lesson を登録し、
    スラッシュコマンドを Discord に同期する関数を返す (on_ready で一度呼ぶ)."""
    cfg = LessonConfig.from_module(config)
    if cfg is None:
        logging.info("LESSON_ROOT / LESSON_USERS が未設定のため /lesson は無効です。")
        return None
    lessons = Lessons(cfg)
    tree = app_commands.CommandTree(client)
    guild = discord.Object(id=cfg.guild_id) if cfg.guild_id else None

    async def lesson(interaction: discord.Interaction) -> None:
        await lessons.start(interaction)

    command = app_commands.Command(
        name="lesson",
        description="前回の振り返りをして、次のレッスンを生成します",
        callback=lesson,
    )
    tree.add_command(command, guild=guild)

    async def sync() -> None:
        try:
            synced = await tree.sync(guild=guild)
            logging.info(
                f"スラッシュコマンドを同期しました: {[c.name for c in synced]}"
            )
        except discord.DiscordException as e:
            logging.error(f"スラッシュコマンドの同期に失敗しました: {e}")

    return sync
