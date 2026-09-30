"""/lesson: 前回レッスンの振り返り (と読みカード) → report → 次のレッスン生成 → 投稿.
/lesson-auto: 振り返りも自己申告もせず生成 → 投稿 (auto モード: できた前提でペースが上がる).

language-learning-audio (submodule: external/language-learning-audio) の CLI を
subprocess で呼ぶ。永続化するのはユーザーごとの learner.json と、Discord 振り返りの
キュー pending_review.json (src/review_queue.py)、読みカードの予定 reading_queue.json
(src/reading.py)、フィードバック用のレッスンの記録 (src/feedback.py) で、作業ディレクトリの
生成物は投稿後に消す。

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

from . import feedback, reading, trip, version
from .review_queue import Entry, ReviewQueue

LLA_DIR = (
    Path(__file__).resolve().parent.parent / "external" / "language-learning-audio"
)
RESULTS = {"ok": "言えた", "shaky": "迷った", "failed": "言えなかった"}
REVIEW_INTRO = (
    "これから定着度チェックです（前回までの表現、全{n}問）。問いを見て声に出して答えてから"
    "「答えを見る」で確かめ、言えた／迷った／言えなかったを選んでください。"
)
READING_INTRO = "続けて読みカードが{n}枚あります（書いてあるものを声に出して読む）。"
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
    # 振り返りの最後に出す読みカードの枚数. 問いをその分減らすので振り返りの時間は増えない.
    # 0 なら出さない
    reading_cards: int = 5
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
            reading_cards=getattr(
                config, "LESSON_READING_CARDS", defaults.reading_cards
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

    def trip_path(self, name: str) -> Path:
        """その人だけの旅程のプロフィール (手で置く. bot は中身を読まず、CLI に渡すだけ).
        あればチャンネルのトピックの設定より優先する."""
        return self.user_dir(name) / "trip.toml"

    @property
    def question_limit(self) -> int:
        """読みカードを出すときの問いの上限の目安 (review_limit からカードの分を引く)."""
        if self.review_limit <= 0 or self.reading_cards <= 0:
            return self.review_limit
        return max(self.review_limit - self.reading_cards, 1)

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
        self, name: str, auto: bool = False, trip: Path | None = None
    ) -> list[str]:
        """trip: 旅程のプロフィール (Lessons.generate_and_post が trip.resolve で決める)."""
        extra = list(self.extra_args)
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


# ---------------------------------------------------------------- review data


@dataclass
class ReviewSession:
    """今回の振り返り: キューから選んだ問いを 1 問ずつ、続けて読みカード (cards) を 1 枚ずつ.
    答えるたびにキュー (読みカードは reading_queue.json) へ書き込むので、途中で時間切れに
    なっても答えた分は残り、残りは未回答のまま次回へ回る."""

    queue: ReviewQueue
    keys: list[str]
    path: Path
    today: date
    cards: list[dict] = field(default_factory=list)
    reading_queue: reading.ReadingQueue | None = None
    reading_path: Path | None = None
    results: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.keys) + len(self.cards)

    @property
    def done(self) -> bool:
        return len(self.results) >= self.total

    def entry(self, n: int) -> Entry:
        return self.queue.entries[self.keys[n]]

    def current_card(self) -> dict | None:
        """今が読みカードならそのカード."""
        n = len(self.results) - len(self.keys)
        return self.cards[n] if 0 <= n < len(self.cards) else None

    def rate(self, result: str) -> None:
        if result not in RESULTS:
            raise ValueError(result)
        if self.done:
            return
        card = self.current_card()
        if card is None:
            self.queue.record(self.keys[len(self.results)], result, self.today)
            self.queue.save(self.path)
        elif self.reading_queue is not None and self.reading_path is not None:
            self.reading_queue.record(card["id"], result, self.today)
            self.reading_queue.save(self.reading_path)
        self.results.append(result)

    @property
    def question_results(self) -> list[str]:
        return self.results[: len(self.keys)]

    @property
    def card_results(self) -> list[str]:
        return self.results[len(self.keys) :]

    def _ids(self, result: str) -> list[str]:
        ids: list[str] = []
        for n, r in enumerate(self.question_results):
            if r == result:
                ids += [i for i in self.entry(n).items if i not in ids]
        return ids

    def failed_ids(self) -> list[str]:
        return self._ids("failed")

    def shaky_ids(self) -> list[str]:
        return self._ids("shaky")

    def render(self, revealed: bool = False, speak: bool = False) -> str:
        """今の問い. 答えは ``revealed`` のときだけ載せる: スポイラー (||…||) は PC 版の
        Discord がメッセージ単位で「開いた」状態を覚えていて、同じメッセージを次の問いに
        編集しても開いたままになるため、ボタンで出す. speak: 🔊 ボタンがある."""
        card = self.current_card()
        if card is not None:
            n = len(self.card_results) + 1
            text = reading.render_card(card, n, len(self.cards), revealed, speak)
        else:
            e = self.entry(len(self.results))
            text = (
                f"**振り返り {len(self.results) + 1}/{len(self.keys)}**"
                f"（レッスン{e.source_lesson}）\n{e.prompt}"
            ) + (f"\n答え: **{e.answer}**" if revealed else "")
        if not self.results:
            intro = REVIEW_INTRO.format(n=len(self.keys))
            if self.cards:
                intro += READING_INTRO.format(n=len(self.cards))
            text = intro + "\n\n" + text
        return text

    def summary(self) -> str:
        answered = self.question_results
        lines = [f"**振り返り**: {len(answered)}/{len(self.keys)}問に回答"]
        for result in ("failed", "shaky"):
            answers = [
                self.entry(n).answer for n, r in enumerate(answered) if r == result
            ]
            if answers:
                lines.append(f"{RESULTS[result]}: {'、'.join(dict.fromkeys(answers))}")
        if self.shaky_ids():
            lines.append(
                "（迷った項目は言えなかった扱いにはせず、早めにもう一度確認します）"
            )
        left = len(self.keys) - len(answered)
        if left:
            lines.append(f"未回答の {left} 問は次回に回します。")
        if self.card_results:
            counts = "・".join(
                f"{RESULTS[r]} {self.card_results.count(r)}"
                for r in ("ok", "shaky", "failed")
                if r in self.card_results
            )
            lines.append(
                f"**読み**: {len(self.card_results)}/{len(self.cards)}枚（{counts}）"
            )
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
            cards: list[dict] = []
            rq: reading.ReadingQueue | None = None
            responded = False
            if self.cfg.reading_cards > 0:
                # デッキは CLI から読むので、Discord の応答期限 (3 秒) に先に返事をしておく
                await interaction.response.send_message("振り返りを準備しています…")
                responded = True
                rq, cards = await self.pick_cards(name, queue, today, channel)
                if cards and self.cfg.review_limit > 0:
                    # 問いをカードの分だけ減らす: 振り返りの時間は増やさない
                    keys = queue.select(today, self.cfg.review_limit - len(cards))
            session = ReviewSession(
                queue, keys, path, today, cards, rq, self.cfg.reading_path(name)
            )

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

            view = ReviewView(
                session, interaction.user.id, finish, expire, speak=self.speak
            )
            if responded:
                view.message = await interaction.edit_original_response(
                    content=session.render(), view=view
                )
                handed_to_view = True
            else:
                await interaction.response.send_message(session.render(), view=view)
                handed_to_view = True
                view.message = await interaction.original_response()
            # 前回届かなかった報告があれば、振り返りの間に送り直す (同じ queue を使うので、
            # その間に答えた分と食い違わない)
            await self.guarded(channel, self.flush_reports(channel, name, queue, path))
        finally:
            if not handed_to_view:
                self.busy.discard(name)

    async def pick_cards(
        self, name: str, queue: ReviewQueue, today: date, channel: Any = None
    ) -> tuple[reading.ReadingQueue, list[dict]]:
        """今回の読みカード. 枚数は reading_cards までで、直前のレッスンの新出の問い
        (必ず出す. なくても 1 問は残す) と合わせて review_limit を超えない分だけ."""
        n = self.cfg.reading_cards
        if self.cfg.review_limit > 0:
            n = min(n, self.cfg.review_limit - max(len(queue.must_answer()), 1))
        rq = reading.ReadingQueue.load(self.cfg.reading_path(name))
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
            return await self._reading_deck(args)

    async def _reading_deck(self, args: list[str]) -> list[dict]:
        try:
            rc, out, _ = await asyncio.wait_for(run_cli(self.cfg, args), timeout=60)
            if rc == 0:
                return reading.parse_deck(out)
            logging.warning(f"読みカードのデッキを読めませんでした (rc={rc})")
        except Exception as e:
            logging.warning(
                f"読みカードのデッキを読めませんでした ({type(e).__name__})"
            )
        return []

    @contextlib.asynccontextmanager
    async def speak(self, card: dict) -> AsyncIterator[Path]:
        """カードの 🔊 の mp3. 公開デッキのカードはキャッシュし、自分の地名のカード
        (own) は一時ファイルにして送ったら消す."""
        voice = reading.profile_voice(self.cfg.lla_dir / self.cfg.profile)
        if card.get("own"):
            with tempfile.TemporaryDirectory() as td:
                out = Path(td) / "reading.mp3"
                await reading.synthesize(card["text"], voice, out)
                yield out
            return
        cache = self.cfg.reading_tts_dir()
        cache.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(f"{voice}|{card['text']}".encode()).hexdigest()
        out = cache / f"{key}.mp3"
        if not out.exists():
            tmp = cache / f"{key}.tmp.mp3"
            await reading.synthesize(card["text"], voice, tmp)
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

    async def generate_and_post(
        self, channel: discord.abc.Messageable, name: str, auto: bool = False
    ) -> None:
        with trip.resolve(self.cfg.trip_path(name), channel) as (source, warning):
            if warning:
                await channel.send(warning)
            await self._generate_and_post(channel, name, auto, source)

    async def _generate_and_post(
        self,
        channel: discord.abc.Messageable,
        name: str,
        auto: bool,
        source: trip.TripSource | None,
    ) -> None:
        work = self.cfg.work_dir(name)
        cleanup(work)
        work.mkdir(parents=True, exist_ok=True)
        learner = self.cfg.learner_path(name)
        # 生成前の learner.json: レッスンの記録に残し、選ばれ方を後から再現できるように
        learner_before = learner.read_bytes() if learner.exists() else None
        args = self.cfg.generate_args(name, auto, source.path if source else None)
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
            await self.post(channel, work, plan, note, owner, manifest, guide)
        finally:
            cleanup(work)

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
        new = "、".join(i["target"] or i["id"] for i in plan.get("new_items", []))
        reviewed = len(plan.get("reviewed_items", []))
        minutes = plan.get("summary", {}).get("duration_s", 0) / 60
        text = (
            f"**レッスン {n}**（約{minutes:.0f}分）\n新出: {new or 'なし'}\n"
            f"復習: {reviewed}項目" + (f"\n{review}" if review else "") + audio_note
        )
        if guide:
            text += "\n\n" + "\n".join(guide)
        try:
            if manifest:
                view = feedback.feedback_view(owner, manifest)
                await channel.send(text, files=files, view=view)
            else:
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
        # 読みカードの 🔊: カード → mp3 のパスを渡す async context manager. None ならボタンなし
        speak: (
            Callable[[dict], contextlib.AbstractAsyncContextManager[Path]] | None
        ) = None,
    ) -> None:
        super().__init__(timeout=1800)
        self.session = session
        self.owner_id = owner_id
        self.finish = finish
        self.expire = expire
        self.speak = speak
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
            if self.speak is not None and self.session.current_card() is not None:
                listen: discord.ui.Button = discord.ui.Button(
                    label="🔊", style=discord.ButtonStyle.secondary
                )
                listen.callback = self._speak  # type: ignore[method-assign]
                self.add_item(listen)
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
            content=self.session.render(revealed=True, speak=self.speak is not None),
            view=self,
        )

    async def _speak(self, interaction: discord.Interaction) -> None:
        """今の読みカードの発音を、押した本人にだけ mp3 で送る. 振り返りは進めない."""
        card = self.session.current_card()
        if self.is_finished() or card is None or self.speak is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            async with self.speak(card) as mp3:
                await interaction.followup.send(
                    file=discord.File(mp3, filename="reading.mp3"), ephemeral=True
                )
        except Exception as e:
            logging.warning(f"読みカードの音声を作れませんでした ({type(e).__name__})")
            await interaction.followup.send("音声を作れませんでした。", ephemeral=True)

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
    """config に LESSON_ROOT と LESSON_USERS があれば /lesson, /lesson-auto,
    /lesson-feedback (-report, -export), /version と、レッスン投稿のフィードバック
    ボタンを登録し、スラッシュコマンドを Discord に同期する関数を返す (on_ready で一度呼ぶ)."""
    cfg = LessonConfig.from_module(config)
    if cfg is None:
        logging.info("LESSON_ROOT / LESSON_USERS が未設定のため /lesson は無効です。")
        return None
    lessons = Lessons(cfg)
    fb = feedback.Feedback(cfg.users, cfg.user_dir, cfg.channel_id)
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
