"""/lesson: 前回レッスンの振り返り → report → 次のレッスン生成 → 投稿.
/lesson-auto: 振り返りも自己申告もせず生成 → 投稿 (auto モード: できた前提でペースが上がる).

language-learning-audio (submodule: external/language-learning-audio) の CLI を
subprocess で呼ぶ。永続化するのはユーザーごとの learner.json と、Discord 振り返りの
キュー pending_review.json (src/review_queue.py) だけで、生成物は投稿後に消す。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

import discord
from discord import app_commands

from .review_queue import Entry, ReviewQueue

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
    review_limit: int = 20  # 1 回の振り返りの最大問数. 0 なら期限の来ている問いすべて
    timeout_min: float = 60
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
            timeout_min=getattr(config, "LESSON_TIMEOUT_MIN", defaults.timeout_min),
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

    def cache_dir(self, name: str) -> Path:
        """keep_cache なら全員で共有する TTS キャッシュ、そうでなければ今回だけの作業用.

        キャッシュのファイル名は音声エンジン・声・速さ・言語・文のハッシュなので、
        学習者をまたいで共有しても混ざらない。"""
        if self.keep_cache:
            return self.root / "tts-cache"
        return self.work_dir(name) / "cache"

    def generate_args(self, name: str, auto: bool = False) -> list[str]:
        extra = list(self.extra_args)
        if auto and "--auto" not in extra:
            extra.append("--auto")
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
            "--cache",
            str(self.cache_dir(name)),
            *extra,
        ]

    def report_args(
        self,
        name: str,
        failed: list[str],
        lesson: int | None = None,
        hesitated: list[str] | None = None,
        recalled: list[str] | None = None,
    ) -> list[str]:
        """lesson を省くと最新のレッスンへの報告になる. 振り返りでは出題元のレッスンを
        必ず渡す (flush_reports). 迷った / 言えたも送る: 音声レッスン側は言えなかった・
        迷った・言えたで次の復習を変える (issue #119)."""
        args = ["report", "--learner", str(self.learner_path(name))]
        if lesson is not None:
            args += ["--lesson", str(lesson)]
        for flag, ids in (
            ("--failed", failed),
            ("--hesitated", hesitated),
            ("--recalled", recalled),
        ):
            if ids:
                args += [flag, ",".join(ids)]
        return args


# ---------------------------------------------------------------- review data


@dataclass
class ReviewSession:
    """今回の振り返り: キューから選んだ問いを 1 問ずつ. 答えるたびにキューへ書き込むので、
    途中で時間切れになっても答えた分は残り、残りは未回答のまま次回へ回る."""

    queue: ReviewQueue
    keys: list[str]
    path: Path
    today: date
    results: list[str] = field(default_factory=list)

    @property
    def done(self) -> bool:
        return len(self.results) >= len(self.keys)

    def entry(self, n: int) -> Entry:
        return self.queue.entries[self.keys[n]]

    def rate(self, result: str) -> None:
        if result not in RESULTS:
            raise ValueError(result)
        if self.done:
            return
        self.queue.record(self.keys[len(self.results)], result, self.today)
        self.queue.save(self.path)
        self.results.append(result)

    def _ids(self, result: str) -> list[str]:
        ids: list[str] = []
        for n, r in enumerate(self.results):
            if r == result:
                ids += [i for i in self.entry(n).items if i not in ids]
        return ids

    def failed_ids(self) -> list[str]:
        return self._ids("failed")

    def shaky_ids(self) -> list[str]:
        return self._ids("shaky")

    def render(self, revealed: bool = False) -> str:
        """今の問い. 答えは ``revealed`` のときだけ載せる: スポイラー (||…||) は PC 版の
        Discord がメッセージ単位で「開いた」状態を覚えていて、同じメッセージを次の問いに
        編集しても開いたままになるため、ボタンで出す."""
        e = self.entry(len(self.results))
        text = (
            f"**振り返り {len(self.results) + 1}/{len(self.keys)}**"
            f"（レッスン{e.source_lesson}）\n{e.prompt}"
        )
        return text + (f"\n答え: **{e.answer}**" if revealed else "")

    def summary(self) -> str:
        lines = [f"**振り返り**: {len(self.results)}/{len(self.keys)}問に回答"]
        for result in ("failed", "shaky"):
            answers = [
                self.entry(n).answer for n, r in enumerate(self.results) if r == result
            ]
            if answers:
                lines.append(f"{RESULTS[result]}: {'、'.join(dict.fromkeys(answers))}")
        if self.shaky_ids():
            lines.append(
                "（迷った項目は言えなかった扱いにはせず、早めにもう一度確認します）"
            )
        left = len(self.keys) - len(self.results)
        if left:
            lines.append(f"未回答の {left} 問は次回に回します。")
        return "\n".join(lines)


def review_note(queue: ReviewQueue, day: date, limit: int) -> str:
    """投稿に添える振り返りの見通し. ``day`` は次に振り返る日."""
    pending = queue.due_count(day)
    if not pending:
        return ""
    # 直前のレッスンの新出は上限を超えても全部出る (ReviewQueue.select)
    asked = max(len(queue.must_answer()), min(pending, limit)) if limit > 0 else pending
    return f"Discord 振り返り: 次回 {asked}問（確認待ち {pending}件）"


# ---------------------------------------------------------------- files


def latest_plan(work: Path) -> Path | None:
    plans = sorted(work.glob("lesson-*.plan.json"))
    return plans[-1] if plans else None


def cleanup(work: Path) -> None:
    """生成物を消す (共有 TTS キャッシュは work の外にあるので残る)."""
    if not work.exists():
        return
    for p in work.iterdir():
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


SYNTH_RE = re.compile(r"synthesized (\d+)/(\d+)")


async def run_cli(
    cfg: LessonConfig,
    args: list[str],
    on_progress: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[int, str, str]:
    """CLI を実行する。stderr の進捗行 (「synthesized 120/450 …」) を on_progress に渡し、
    cfg.timeout_min を超えたら止めて rc=-1 を返す."""
    proc = await asyncio.create_subprocess_exec(
        cfg.python,
        "-m",
        "audiolesson.cli",
        *args,
        cwd=str(cfg.lla_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None and proc.stderr is not None
    err_chunks: list[bytes] = []

    async def read_stderr(stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(4096):
            err_chunks.append(chunk)
            lines = re.split(r"[\r\n]+", chunk.decode(errors="replace").strip())
            if on_progress and lines[-1]:
                await on_progress(lines[-1])

    try:
        out, _ = await asyncio.wait_for(
            asyncio.gather(proc.stdout.read(), read_stderr(proc.stderr)),
            timeout=cfg.timeout_min * 60,
        )
        await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        err = b"".join(err_chunks).decode(errors="replace")
        return -1, "", f"{cfg.timeout_min:g} 分で終わらなかったので止めました。\n{err}"
    return (
        proc.returncode or 0,
        out.decode(errors="replace"),
        b"".join(err_chunks).decode(errors="replace"),
    )


class StatusMessage:
    """生成中の経過をチャンネルの 1 通のメッセージに書き換えて出す (間引いて編集)."""

    def __init__(self, channel: discord.abc.Messageable, interval: float = 15) -> None:
        self.channel = channel
        self.interval = interval
        self.message: discord.Message | None = None
        self.started = time.monotonic()
        self.last_edit = 0.0
        self.detail = ""

    def text(self, head: str = "生成中…") -> str:
        m, sec = divmod(int(time.monotonic() - self.started), 60)
        return f"{head}（経過 {m}:{sec:02d}）" + (
            f" {self.detail}" if self.detail else ""
        )

    async def start(self) -> None:
        self.message = await self.channel.send(self.text())

    async def update(self, line: str) -> None:
        match = SYNTH_RE.search(line)
        if match:
            self.detail = f"音声合成 {match[1]}/{match[2]}"
        if self.message is None or time.monotonic() - self.last_edit < self.interval:
            return
        self.last_edit = time.monotonic()
        try:
            await self.message.edit(content=self.text())
        except discord.DiscordException:
            pass

    async def finish(self, ok: bool) -> None:
        if self.message is not None:
            self.detail = ""
            try:
                await self.message.edit(
                    content=self.text("生成完了" if ok else "生成できませんでした")
                )
            except discord.DiscordException:
                pass


def _tail(text: str, limit: int = 1500) -> str:
    return text[-limit:] if len(text) > limit else text


# ---------------------------------------------------------------- flow


class Lessons:
    def __init__(
        self, cfg: LessonConfig, today: Callable[[], date] = date.today
    ) -> None:
        self.cfg = cfg
        self.today = today
        self.busy: set[str] = set()

    async def start(self, interaction: discord.Interaction, auto: bool = False) -> None:
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
            today = self.today()
            path = self.cfg.pending_path(name)
            queue = ReviewQueue.load(path, today)
            if not auto and queue.drop_stale_new():
                queue.save(path)
            keys = [] if auto else queue.select(today, self.cfg.review_limit)
            if not keys:
                note = "（自動モード: 振り返りなし）" if auto else ""
                await interaction.response.send_message(
                    f"レッスンを生成しています…{note}"
                )

                async def report_then_generate() -> None:
                    # 前回届かなかった報告があれば、生成の前に送る
                    if await self.flush_reports(channel, name, queue, path):
                        await self.generate_and_post(channel, name, auto=auto)

                await self.guarded(channel, report_then_generate())
                return
            session = ReviewSession(queue, keys, path, today)

            async def finish(generate: bool) -> None:
                async def run() -> None:
                    if not await self.flush_reports(channel, name, queue, path):
                        return
                    if generate:
                        await self.generate_and_post(channel, name)

                try:
                    await self.guarded(channel, run())
                finally:
                    self.busy.discard(name)

            async def expire() -> None:
                await finish(False)  # 答えた分だけ報告し、生成はしない

            view = ReviewView(session, interaction.user.id, finish, expire)
            await interaction.response.send_message(session.render(), view=view)
            handed_to_view = True
            view.message = await interaction.original_response()
            # 前回届かなかった報告があれば、振り返りの間に送り直す (同じ queue を使うので、
            # その間に答えた分と食い違わない)
            await self.guarded(channel, self.flush_reports(channel, name, queue, path))
        finally:
            if not handed_to_view:
                self.busy.discard(name)

    @staticmethod
    async def guarded(channel: discord.abc.Messageable, work: Awaitable[Any]) -> None:
        """想定外の例外もチャンネルに知らせる (ボタンのコールバックでは握りつぶされるため)."""
        try:
            await work
        except Exception as e:
            logging.exception("/lesson の処理中にエラーが発生しました")
            await channel.send(f"エラーが発生しました: {e}")

    async def flush_reports(
        self,
        channel: discord.abc.Messageable,
        name: str,
        queue: ReviewQueue,
        path: Path,
    ) -> bool:
        """キューに残っている報告を、出題元のレッスンごとに audiolesson report で送る.

        届いた分だけキューから消すので、失敗した分は次の /lesson で送り直す (一度届いた
        報告は二度送らない). 最新のレッスンが「報告済み」になるのは、そのレッスンの問いに
        答えたときだけ. すべて届けば True."""
        for lesson, outcome in queue.reports():
            args = self.cfg.report_args(
                name,
                outcome["failed"],
                lesson=lesson,
                hesitated=outcome["shaky"],
                recalled=outcome["ok"],
            )
            rc, out, err = await run_cli(self.cfg, args)
            if rc != 0:
                await channel.send(
                    f"レッスン{lesson}の結果を report できませんでした"
                    f"（次の /lesson で送り直します）:\n```\n{_tail(err or out)}\n```"
                )
                return False
            queue.mark_reported(lesson, outcome)
            queue.save(path)
        return True

    async def generate_and_post(
        self, channel: discord.abc.Messageable, name: str, auto: bool = False
    ) -> None:
        work = self.cfg.work_dir(name)
        cleanup(work)
        work.mkdir(parents=True, exist_ok=True)
        try:
            status = StatusMessage(channel)
            await status.start()
            async with channel.typing():
                rc, out, err = await run_cli(
                    self.cfg, self.cfg.generate_args(name, auto), status.update
                )
            await status.finish(rc == 0)
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
            # 自動モードでも問いはキューに足す: 次に /lesson を使えばそこで振り返れる
            today = self.today()
            path = self.cfg.pending_path(name)
            queue = ReviewQueue.load(path, today)
            queue.add_from_plan(plan, today)
            queue.save(path)
            tomorrow = today + timedelta(days=1)
            note = "" if auto else review_note(queue, tomorrow, self.cfg.review_limit)
            await self.post(channel, work, plan, note)
        finally:
            cleanup(work)

    async def post(
        self,
        channel: discord.abc.Messageable,
        work: Path,
        plan: dict,
        review: str = "",
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
            f"復習: {reviewed}項目" + (f"\n{review}" if review else "") + audio_note
        )
        try:
            await channel.send(text, files=files)
        finally:
            for f in files:
                f.close()


class ReviewView(discord.ui.View):
    """1問ずつ: 「答えを見る」で答えと評価ボタンを出し、評価すると次の問いに差し替え.

    振り返りを飛ばして生成するボタンはない: 直前のレッスンの新出に全部答えるまで次の
    レッスンは生成しない (答えなければ音声レッスン側は成功とみなすため). 振り返らずに
    生成したいときは /lesson-auto."""

    def __init__(
        self,
        session: ReviewSession,
        owner_id: int,
        finish: Callable[[bool], Awaitable[None]],  # 引数: 続けて生成するか
        expire: Callable[[], Awaitable[None]],
    ) -> None:
        super().__init__(timeout=1800)
        self.session = session
        self.owner_id = owner_id
        self.finish = finish
        self.expire = expire
        self.message: discord.InteractionMessage | None = None
        self._show(revealed=False)

    def _show(self, revealed: bool) -> None:
        """答えの前は「答えを見る」だけ、答えの後は評価ボタン."""
        self.clear_items()
        if revealed:
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
        else:
            reveal: discord.ui.Button = discord.ui.Button(
                label="答えを見る", style=discord.ButtonStyle.primary
            )
            reveal.callback = self._reveal  # type: ignore[method-assign]
            self.add_item(reveal)

    async def _reveal(self, interaction: discord.Interaction) -> None:
        if self.is_finished() or self.session.done:
            return
        self._show(revealed=True)
        await interaction.response.edit_message(
            content=self.session.render(revealed=True), view=self
        )

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
                self._show(revealed=False)
                await interaction.response.edit_message(
                    content=self.session.render(), view=self
                )
                return
            self.stop()
            await interaction.response.edit_message(
                content=self.session.summary() + "\n\n次のレッスンを生成しています…",
                view=None,
            )
            await self.finish(True)

        return callback

    async def on_timeout(self) -> None:
        await self.expire()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="時間切れです。答えた分は記録しました。"
                    "未回答の問いは次の /lesson で出します。",
                    view=None,
                )
            except discord.DiscordException:
                pass


def setup(client: discord.Client, config: Any) -> Callable[[], Awaitable[None]] | None:
    """config に LESSON_ROOT と LESSON_USERS があれば /lesson と /lesson-auto を登録し、
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

    async def lesson_auto(interaction: discord.Interaction) -> None:
        await lessons.start(interaction, auto=True)

    for command in (
        app_commands.Command(
            name="lesson",
            description="前回の振り返りをして、次のレッスンを生成します",
            callback=lesson,
        ),
        app_commands.Command(
            name="lesson-auto",
            description="振り返りなしで次のレッスンを生成します（できた前提でペースが上がる）",
            callback=lesson_auto,
        ),
    ):
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
