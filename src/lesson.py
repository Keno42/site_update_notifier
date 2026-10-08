"""/lesson: 前回レッスンの振り返り (src/review.py) → report → 次のレッスン生成 → 投稿.
/lesson-auto: 振り返りも自己申告もせず生成 → 投稿 (auto モード: できた前提でペースが上がる).

language-learning-audio (submodule: external/language-learning-audio) の CLI を
subprocess で呼ぶ. 永続化するのはユーザーごとの learner.json、振り返りのキュー
pending_review.json (src/review_queue.py)、カードの予定 scene_queue.json /
reading_queue.json (src/cards.py)、フィードバック用のレッスンの記録 (src/feedback.py) で、
作業ディレクトリの生成物は投稿後に消す. カードのデッキは毎回 CLI から読み、保存しない.

旅程のプロフィール (language-learning-audio #132) があれば、生成に --trip で渡す (旅行の
can-do 項目を先に教える順番になる. ペースは変わらない). ユーザーのディレクトリの trip.toml、
なければチャンネルのトピックの [trip] (src/trip.py). レッスンの記録に残すのはその sha256 だけ.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import discord
from discord import app_commands

from . import feedback, levers, newlist, reading, scenes, speech, trip, version, weekly
from .cards import CardQueue
from .review import ReviewSession, ReviewView, log_review, review_note
from .review_queue import ReviewQueue

LLA_DIR = (
    Path(__file__).resolve().parent.parent / "external" / "language-learning-audio"
)
FEEDBACK_GUIDE = "聞き終えたら「フィードバック」ボタン（または /lesson-feedback）で手応えを記録してください（30秒ほど）。"


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
    # 振り返りの後半に出す場面カード・読みカードの枚数. 問いをその分減らすので振り返りの
    # 時間は増えない. 0 なら出さない
    scene_cards: int = 3
    reading_cards: int = 3
    # 場面ごとの準備状況をレッスンの投稿に添える間隔 (日). 0 なら添えない
    readiness_days: int = 7
    # 旅程の設定 (trip.toml / チャンネルのトピック) の地名 (places) も読みカードにする.
    # カードは振り返りのチャンネルに出るので、trip.toml の地名を他の人に見せたくないなら False に
    reading_own_places: bool = True
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
            scene_cards=getattr(config, "LESSON_SCENE_CARDS", defaults.scene_cards),
            reading_cards=getattr(
                config, "LESSON_READING_CARDS", defaults.reading_cards
            ),
            readiness_days=getattr(
                config, "LESSON_READINESS_DAYS", defaults.readiness_days
            ),
            reading_own_places=getattr(
                config, "LESSON_READING_OWN_PLACES", defaults.reading_own_places
            ),
        )

    def user_dir(self, name: str) -> Path:
        if not name or "/" in name or name in (".", ".."):
            raise ValueError(f"learner name must be a plain name: {name!r}")
        return self.root / name

    def learner_path(self, name: str) -> Path:
        return self.user_dir(name) / "learner.json"

    def pending_path(self, name: str) -> Path:
        return self.user_dir(name) / "pending_review.json"

    def reading_path(self, name: str) -> Path:
        return self.user_dir(name) / "reading_queue.json"

    def scene_path(self, name: str) -> Path:
        return self.user_dir(name) / "scene_queue.json"

    def readiness_path(self, name: str) -> Path:
        return self.user_dir(name) / "readiness_reminder.json"

    def trip_path(self, name: str) -> Path:
        """その人だけの旅程のプロフィール (手で置く. bot は中身を読まず、CLI に渡すだけ).
        あればチャンネルのトピックの設定より優先する."""
        return self.user_dir(name) / "trip.toml"

    @property
    def question_limit(self) -> int:
        """カードを出すときの問いの上限の目安 (review_limit からカードの分を引く)."""
        cards = max(self.scene_cards, 0) + max(self.reading_cards, 0)
        if self.review_limit <= 0 or cards <= 0:
            return self.review_limit
        return max(self.review_limit - cards, 1)

    def reading_tts_dir(self) -> Path:
        """公開デッキのカードの 🔊 音声のキャッシュ (自分の地名のカードは保存しない)."""
        return self.root / "reading-tts"

    def work_dir(self, name: str) -> Path:
        return self.user_dir(name) / "work"

    def cache_dir(self, name: str) -> Path:
        """keep_cache なら全員で共有する TTS キャッシュ、そうでなければ今回だけの作業用.

        キャッシュのファイル名は音声エンジン・声・速さ・言語・文のハッシュなので、
        学習者をまたいで共有しても混ざらない。"""
        if self.keep_cache:
            return self.root / "tts-cache"
        return self.work_dir(name) / "cache"

    def generate_args(
        self,
        name: str,
        auto: bool = False,
        trip: Path | None = None,
        lever_args: list[str] | None = None,
    ) -> list[str]:
        """trip: 旅程のプロフィール (Lessons.generate_and_post が trip.resolve で決める).
        lever_args: チャンネルのトピックのレバー (src/levers.py). None なら extra_args のまま."""
        extra = levers.apply(self.extra_args, lever_args)
        if auto and "--auto" not in extra:
            extra.append("--auto")
        if trip is not None and "--trip" not in extra:
            extra += ["--trip", str(trip)]
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
            cards: list[dict] = []
            picked_scenes: list[dict] = []
            rq: CardQueue | None = None
            sq: CardQueue | None = None
            responded = False
            if self.cfg.reading_cards > 0 or self.cfg.scene_cards > 0:
                # カードは CLI から読むので、Discord の応答期限 (3 秒) に先に返事をしておく
                await interaction.response.send_message("振り返りを準備しています…")
                responded = True
                sq, picked_scenes = await self.pick_scenes(name, queue, today, channel)
                rq, cards = await self.pick_cards(
                    name, queue, today, channel, reserved=len(picked_scenes)
                )
                shown = len(cards) + len(picked_scenes)
                if shown and self.cfg.review_limit > 0:
                    # 問いをカードの分だけ減らす: 振り返りの時間は増やさない
                    keys = queue.select(today, self.cfg.review_limit - shown)
            session = ReviewSession(
                queue,
                keys,
                path,
                today,
                cards,
                rq,
                self.cfg.reading_path(name),
                scenes=picked_scenes,
                scene_queue=sq,
                scene_path=self.cfg.scene_path(name),
            )

            async def finish(generate: bool) -> None:
                log_review(
                    self.cfg.user_dir(name),
                    session.timing_record(
                        datetime.now().astimezone(), finished=session.done
                    ),
                )

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

            view = ReviewView(
                session, interaction.user.id, finish, expire, speak=self.speak
            )
            if responded:
                view.message = await interaction.edit_original_response(
                    content=session.render(speak=True), view=view
                )
                handed_to_view = True
            else:
                await interaction.response.send_message(
                    session.render(speak=True), view=view
                )
                handed_to_view = True
                view.message = await interaction.original_response()
            # 前回届かなかった報告があれば、振り返りの間に送り直す (同じ queue を使うので、
            # その間に答えた分と食い違わない)
            await self.guarded(channel, self.flush_reports(channel, name, queue, path))
        finally:
            if not handed_to_view:
                self.busy.discard(name)

    def _card_room(self, queue: ReviewQueue, wanted: int, reserved: int = 0) -> int:
        """カードに使える枚数: wanted までで、直前のレッスンの新出の問い (必ず出す. なくても
        1 問は残す) と先に決まったカード (reserved) と合わせて review_limit を超えない分."""
        if self.cfg.review_limit <= 0:
            return wanted
        room = self.cfg.review_limit - max(len(queue.must_answer()), 1) - reserved
        return max(min(wanted, room), 0)

    async def pick_scenes(
        self, name: str, queue: ReviewQueue, today: date, channel: Any = None
    ) -> tuple[CardQueue, list[dict]]:
        """今回の場面カード (scene_cards 枚まで). 読みカードより先に枠を取る."""
        sq = CardQueue.load(self.cfg.scene_path(name))
        n = self._card_room(queue, self.cfg.scene_cards)
        if n <= 0:
            return sq, []
        deck = await self.scene_deck(name, channel)
        return sq, sq.select(deck, today, n)

    async def pick_cards(
        self,
        name: str,
        queue: ReviewQueue,
        today: date,
        channel: Any = None,
        reserved: int = 0,
    ) -> tuple[CardQueue, list[dict]]:
        """今回の読みカード (reading_cards 枚まで、場面カードの残りの枠で)."""
        n = self._card_room(queue, self.cfg.reading_cards, reserved)
        rq = CardQueue.load(self.cfg.reading_path(name))
        if n <= 0:
            return rq, []
        deck = await self.reading_deck(name, channel)
        return rq, rq.select(deck, today, n)

    async def reading_deck(self, name: str, channel: Any = None) -> list[dict]:
        """``audiolesson reading`` のデッキ (メモリ上だけ). 読めなければ空で、振り返りは
        問いだけで続ける. 旅程の地名が入りうるので、エラーの中身もログに出さない.
        旅程の設定を読めないときの案内は生成のときに出す."""
        with trip.resolve(self.cfg.trip_path(name), channel) as (source, _):
            args = ["reading", self.cfg.curriculum]
            if self.cfg.reading_own_places and source is not None:
                args += ["--trip", str(source.path)]
            return await self._deck(args, reading.parse_deck, "読みカード")

    async def scene_deck(
        self, name: str, channel: Any = None, learner: bool = True
    ) -> list[dict]:
        """``audiolesson scenes`` の場面カード (メモリ上だけ). learner: 学んだ項目で
        出題できるものだけ. 旅程の設定は季節 (season) のためだけに渡す."""
        with trip.resolve(self.cfg.trip_path(name), channel) as (source, _):
            return await self._scene_deck(name, source, learner)

    async def _scene_deck(
        self, name: str, source: trip.TripSource | None, learner: bool = True
    ) -> list[dict]:
        args = ["scenes", self.cfg.curriculum]
        if learner:
            args += ["--learner", str(self.cfg.learner_path(name))]
        if source is not None:
            args += ["--trip", str(source.path)]
        return await self._deck(args, scenes.parse_scenes, "場面カード")

    async def _deck(
        self, args: list[str], parse: Callable[[str], list[dict]], label: str
    ) -> list[dict]:
        try:
            rc, out, _ = await asyncio.wait_for(run_cli(self.cfg, args), timeout=60)
            if rc == 0:
                return parse(out)
            logging.warning(f"{label}を読めませんでした (rc={rc})")
        except Exception as e:
            logging.warning(f"{label}を読めませんでした ({type(e).__name__})")
        return []

    @contextlib.asynccontextmanager
    async def speak(self, card: dict) -> AsyncIterator[Path]:
        """カードの 🔊 の mp3. 公開デッキのカードはキャッシュし、自分の地名のカード
        (own) は一時ファイルにして送ったら消す."""
        voice = speech.profile_voice(self.cfg.lla_dir / self.cfg.profile)
        if card.get("own"):
            with tempfile.TemporaryDirectory() as td:
                out = Path(td) / "reading.mp3"
                await speech.synthesize(card["text"], voice, out)
                yield out
            return
        cache = self.cfg.reading_tts_dir()
        cache.mkdir(parents=True, exist_ok=True)
        # v2: 並べた表現のあいだに無音が入った版 (前の版のキャッシュは使わない)
        key = hashlib.sha1(f"v2|{voice}|{card['text']}".encode()).hexdigest()
        out = cache / f"{key}.mp3"
        if not out.exists():
            tmp = cache / f"{key}.tmp.mp3"
            await speech.synthesize(card["text"], voice, tmp)
            tmp.replace(out)
        yield out

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

    async def report_sooner(self, name: str, lesson: int, ids: list[str]) -> bool:
        """フィードバックで「練習が足りなかった・覚えていない」と選んだ項目を、迷った扱いで報告する (#81):
        音声レッスン側で早めに (半分の間隔で) もう一度出る. 届いたか."""
        args = self.cfg.report_args(name, [], lesson=lesson, hesitated=ids)
        rc, out, err = await run_cli(self.cfg, args)
        if rc != 0:
            logging.error("report --hesitated に失敗しました: %s", _tail(err or out))
        return rc == 0

    async def generate_and_post(
        self, channel: discord.abc.Messageable, name: str, auto: bool = False
    ) -> None:
        # 前のレッスンの新出表現の一覧を、フィードバックが来ないまま次の生成が始まるなら、ここで出す (#79)
        await newlist.release(
            channel, newlist.PendingLists(self.cfg.user_dir(name)).take_all()
        )
        lever_args, lever_warning = levers.from_topic(channel)
        if lever_warning:
            await channel.send(lever_warning)
        with trip.resolve(self.cfg.trip_path(name), channel) as (source, warning):
            if warning:
                await channel.send(warning)
            await self._generate_and_post(channel, name, auto, source, lever_args)

    async def _generate_and_post(
        self,
        channel: discord.abc.Messageable,
        name: str,
        auto: bool,
        source: trip.TripSource | None,
        lever_args: list[str] | None = None,
    ) -> None:
        work = self.cfg.work_dir(name)
        cleanup(work)
        work.mkdir(parents=True, exist_ok=True)
        learner = self.cfg.learner_path(name)
        # 生成前の learner.json: レッスンの記録に残し、選ばれ方を後から再現できるように
        learner_before = learner.read_bytes() if learner.exists() else None
        args = self.cfg.generate_args(
            name, auto, source.path if source else None, lever_args
        )
        if source is not None and str(source.path) not in args:
            source = None  # LESSON_EXTRA_ARGS の --trip が優先された
        try:
            status = StatusMessage(channel)
            await status.start()
            async with channel.typing():
                rc, out, err = await run_cli(self.cfg, args, status.update)
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
            manifest = self.save_manifest(
                name,
                work,
                plan,
                learner_before,
                trip.recorded_args(args, source),
                source.sha256 if source else None,
            )
            # 自動モードでも問いはキューに足す: 次に /lesson を使えばそこで振り返れる
            today = self.today()
            path = self.cfg.pending_path(name)
            queue = ReviewQueue.load(path, today)
            queue.add_from_plan(plan, today)
            queue.save(path)
            tomorrow = today + timedelta(days=1)
            note = "" if auto else review_note(queue, tomorrow, self.cfg.question_limit)
            owner = next((u for u, n in self.cfg.users.items() if n == name), 0)
            guide = [FEEDBACK_GUIDE] if manifest else []
            summary = await self.readiness_summary(name, source, today)
            if summary:
                guide.append(summary)
            await self.post(channel, work, plan, note, owner, manifest, guide)
        finally:
            cleanup(work)

    async def readiness_summary(
        self, name: str, source: trip.TripSource | None, today: date
    ) -> str:
        """readiness_days 日ごとに、場面ごとの準備状況 (#129). 出したらその日を記録する.
        カードを読めなければ出さない (次のレッスンで出す)."""
        if self.cfg.readiness_days <= 0:
            return ""
        path = self.cfg.readiness_path(name)
        try:
            last = date.fromisoformat(json.loads(path.read_text("utf-8"))["last"])
        except (OSError, ValueError, KeyError, TypeError):
            last = None
        if last is not None and (today - last).days < self.cfg.readiness_days:
            return ""
        text = await self.readiness_text(name, source)
        if text:
            path.write_text(json.dumps({"last": today.isoformat()}), "utf-8")
        return text

    async def readiness_text(self, name: str, source: trip.TripSource | None) -> str:
        """場面ごとの準備状況 (日数の制限なし). カードを読めなければ空."""
        every = await self._scene_deck(name, source, learner=False)
        if not every:
            return ""
        met = await self._scene_deck(name, source, learner=True)
        queue = CardQueue.load(self.cfg.scene_path(name))
        return scenes.readiness(every, {c["id"] for c in met}, queue)

    async def week(self, interaction: discord.Interaction, days: int = 7) -> None:
        """/lesson-week: 直近の信号を並べたレポート (src/weekly.py). 本人にだけ見える."""
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
        await interaction.response.defer(ephemeral=True, thinking=True)
        days = min(max(days, 1), 60)
        text = weekly.build(self.cfg.user_dir(name), self.today(), days)
        await interaction.followup.send(text, ephemeral=True)
        with trip.resolve(self.cfg.trip_path(name), interaction.channel) as (source, _):
            readiness = await self.readiness_text(name, source)
        if readiness:
            await interaction.followup.send(readiness, ephemeral=True)

    def save_manifest(
        self,
        name: str,
        work: Path,
        plan: dict,
        learner_before: bytes | None,
        args: list[str],
        trip_sha256: str | None = None,
    ) -> str | None:
        """生成したレッスンの記録を残し、その ID (lesson-012 / 再生成なら lesson-012.2)
        を返す (フィードバックの紐付け先, issue #128). 失敗しても投稿は止めない (None).
        旅程のプロフィールは中身を残さず、どの版で生成したかだけ (sha256) を残す."""
        lla = version.head_commit(self.cfg.lla_dir)
        bot = version.INFO.bot
        try:
            d = feedback.Ledger(self.cfg.user_dir(name)).save_manifest(
                work,
                plan,
                learner_before,
                self.cfg.learner_path(name),
                {
                    # bot は動いている版、language-learning-audio は生成に使ったディスク上の版
                    "bot": bot.full if bot else None,
                    "lla": lla.full if lla else None,
                },
                args,
                datetime.now().astimezone(),
                trip_sha256=trip_sha256,
            )
        except OSError:
            logging.exception("レッスンの記録を保存できませんでした")
            return None
        return d.name

    def _keep_new_list(
        self, owner: int, manifest: str, plan: dict, channel: Any, sent: Any
    ) -> None:
        """新出表現の一覧 (#79) は、フィードバックを送ったとき (送らなければ次の生成の始まり) に、この投稿への
        返信として出す: 聞く前に読まないため. 出すまで、ユーザーのディレクトリに置いておく."""
        name = self.cfg.users.get(owner)
        message_id = getattr(sent, "id", None)
        if name is None or message_id is None:
            return
        try:
            newlist.PendingLists(self.cfg.user_dir(name)).add(
                manifest,
                getattr(channel, "id", 0),
                message_id,
                newlist.list_text(plan["lesson_number"], plan.get("new_items", [])),
                plan["lesson_number"],
            )
        except OSError:
            logging.exception("新出表現の一覧を保存できませんでした")

    async def post(
        self,
        channel: discord.abc.Messageable,
        work: Path,
        plan: dict,
        review: str = "",
        owner: int = 0,
        manifest: str | None = None,
        guide: list[str] | None = None,
    ) -> None:
        """owner: レッスンを受けた人の Discord ID (フィードバックボタンを押せる人).
        manifest: レッスンの記録の ID. 記録がなければボタンは付けない."""
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
        reviewed = len(plan.get("reviewed_items", []))
        minutes = plan.get("summary", {}).get("duration_s", 0) / 60
        text = (
            f"**レッスン {n}**（約{minutes:.0f}分）\n{newlist.count_text(plan)}\n"
            f"復習: {reviewed}項目" + (f"\n{review}" if review else "") + audio_note
        )
        if guide:
            text += "\n\n" + "\n".join(guide)
        try:
            if manifest:
                view = feedback.feedback_view(owner, manifest)
                sent = await channel.send(text, files=files, view=view)
                self._keep_new_list(owner, manifest, plan, channel, sent)
            else:
                await channel.send(text, files=files)
        finally:
            for f in files:
                f.close()


def setup(client: discord.Client, config: Any) -> Callable[[], Awaitable[None]] | None:
    """config に LESSON_ROOT と LESSON_USERS があれば /lesson, /lesson-auto,
    /lesson-feedback (-report, -export), /version と、レッスン投稿のフィードバック
    ボタンを登録し、スラッシュコマンドを Discord に同期する関数を返す (on_ready で一度呼ぶ)."""
    cfg = LessonConfig.from_module(config)
    if cfg is None:
        logging.info("LESSON_ROOT / LESSON_USERS が未設定のため /lesson は無効です。")
        return None
    lessons = Lessons(cfg)
    fb = feedback.Feedback(
        cfg.users, cfg.user_dir, cfg.channel_id, report_sooner=lessons.report_sooner
    )
    feedback.FeedbackButton.handler = fb
    client.add_dynamic_items(feedback.FeedbackButton)
    tree = app_commands.CommandTree(client)
    guild = discord.Object(id=cfg.guild_id) if cfg.guild_id else None

    async def lesson(interaction: discord.Interaction) -> None:
        await lessons.start(interaction)

    async def lesson_auto(interaction: discord.Interaction) -> None:
        await lessons.start(interaction, auto=True)

    lesson_option = app_commands.describe(
        lesson="レッスン番号（例: 12。再生成した記録は 12.2。省略すると最新）"
    )

    @lesson_option
    async def lesson_feedback(
        interaction: discord.Interaction, lesson: str | None = None
    ) -> None:
        await fb.open_form(interaction, lesson)

    @lesson_option
    async def feedback_report(
        interaction: discord.Interaction, lesson: str | None = None
    ) -> None:
        await fb.report(interaction, lesson)

    async def lesson_week(interaction: discord.Interaction, days: int = 7) -> None:
        await lessons.week(interaction, days)

    @lesson_option
    async def feedback_export(
        interaction: discord.Interaction, lesson: str | None = None
    ) -> None:
        await fb.export(interaction, lesson)

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
        app_commands.Command(
            name="lesson-feedback",
            description="レッスンの手応えを記録します（30秒ほど）",
            callback=lesson_feedback,
        ),
        app_commands.Command(
            name="lesson-feedback-report",
            description="レッスンのフィードバックの要約を表示します",
            callback=feedback_report,
        ),
        app_commands.Command(
            name="lesson-feedback-export",
            description="レッスンのフィードバックと記録一式を zip で添付します",
            callback=feedback_export,
        ),
        app_commands.Command(
            name="lesson-week",
            description="直近の振り返り・フィードバック・場面の準備状況を並べて表示します",
            callback=app_commands.describe(days="何日分か（既定 7、最大 60）")(
                lesson_week
            ),
        ),
        version.command(),
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
