"""/lesson の振り返り: 問い → 場面カード → 読みカードを 1 つずつ.

問いは pending_review.json (src/review_queue.py)、カードは scene_queue.json /
reading_queue.json (src/cards.py) に、答えるたびに書き込む.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Awaitable, Callable

import discord

from . import reading, scenes
from .interaction import RATE_FAILED, SHOW_FAILED, answers_on_failure, screen_step
from .cards import CardQueue
from .review_queue import Entry, ReviewQueue

REVIEW_LOG = "review_log.jsonl"
# 1 つの問い・カードにかかった時間の上限 (秒). 席を外したぶんを所要時間に数えない
IDLE_CAP_S = 120
RESULTS = {"ok": "言えた", "shaky": "迷った", "failed": "言えなかった"}
REVIEW_INTRO = (
    "これから定着度チェックです（前回までの表現、全{n}問）。問いを見て声に出して答えてから"
    "「答えを見る」で確かめ、言えた／迷った／言えなかったを選んでください。"
)
SCENE_INTRO = "続けて場面カードが{n}枚"
BONUS_NOTE = "聞いただけの文です。言えたらボーナス（言えなくて大丈夫）"
READING_INTRO = "続けて読みカードが{n}枚あります（書いてあるものを声に出して読む）。"


@dataclass
class ReviewSession:
    """今回の振り返り: キューから選んだ問いを 1 問ずつ、続けて場面カード (scenes)、
    読みカード (cards) を 1 枚ずつ. 答えるたびにそれぞれのキュー (scene_queue.json /
    reading_queue.json) へ書き込むので、途中で時間切れになっても答えた分は残り、残りは
    未回答のまま次回へ回る."""

    queue: ReviewQueue
    keys: list[str]
    path: Path
    today: date
    cards: list[dict] = field(default_factory=list)
    reading_queue: CardQueue | None = None
    reading_path: Path | None = None
    results: list[str] = field(default_factory=list)
    scenes: list[dict] = field(default_factory=list)
    scene_queue: CardQueue | None = None
    scene_path: Path | None = None
    clock: Callable[[], float] = time.monotonic
    timings: list[tuple[str, float]] = field(default_factory=list)  # (種類, 秒)
    _shown_at: float | None = field(default=None, repr=False)
    _snapshot: dict[str, Entry] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._shown_at = self.clock()
        # bonus の問いは答えたら外すので、この回の表示用に控えておく
        self._snapshot = {
            k: self.queue.entries[k] for k in self.keys if k in self.queue.entries
        }

    @property
    def total(self) -> int:
        return len(self.keys) + len(self.scenes) + len(self.cards)

    @property
    def done(self) -> bool:
        return len(self.results) >= self.total

    def entry(self, n: int) -> Entry:
        key = self.keys[n]
        return self.queue.entries.get(key) or self._snapshot[key]

    def _step(self) -> tuple[str, int]:
        """今の位置: ("question" | "scene" | "card" | "done", その中での番号)."""
        n = len(self.results)
        for kind, size in (
            ("question", len(self.keys)),
            ("scene", len(self.scenes)),
            ("card", len(self.cards)),
        ):
            if n < size:
                return kind, n
            n -= size
        return "done", 0

    def current_scene(self) -> dict | None:
        """今が場面カードならそのカード."""
        kind, n = self._step()
        return self.scenes[n] if kind == "scene" else None

    def current_card(self) -> dict | None:
        """今が読みカードならそのカード."""
        kind, n = self._step()
        return self.cards[n] if kind == "card" else None

    def rate(self, result: str) -> None:
        if result not in RESULTS:
            raise ValueError(result)
        if self.done:
            return
        kind, n = self._step()
        now = self.clock()
        if self._shown_at is not None:
            self.timings.append((kind, max(0.0, now - self._shown_at)))
        self._shown_at = now
        if kind == "question":
            if self.entry(n).bonus:
                # 言えたときだけ報告、どちらでも外す
                self.queue.record_bonus(self.keys[n], result)
            else:
                self.queue.record(self.keys[n], result, self.today)
            self.queue.save(self.path)
        else:
            cards, queue, path = (
                (self.scenes, self.scene_queue, self.scene_path)
                if kind == "scene"
                else (self.cards, self.reading_queue, self.reading_path)
            )
            if queue is not None and path is not None:
                queue.record(cards[n]["id"], result, self.today)
                queue.save(path)
        self.results.append(result)

    def timing_record(self, now: datetime, finished: bool) -> dict:
        """所要時間の記録 (答えた分だけ). 1 つに IDLE_CAP_S を超えた分は数えない (capped は
        その件数). 中身は件数と秒だけで、何を聞かれたかは入れない."""
        seconds: dict[str, float] = {}
        capped = 0
        for kind, s in self.timings:
            capped += s > IDLE_CAP_S
            seconds[kind] = seconds.get(kind, 0.0) + min(s, IDLE_CAP_S)
        counts = {k: [t[0] for t in self.timings].count(k) for k in seconds}
        record = {
            "ts": now.isoformat(timespec="seconds"),
            "finished": finished,
            "planned": {
                "question": len(self.keys),
                "scene": len(self.scenes),
                "card": len(self.cards),
            },
            "answered": counts,
            "seconds": {k: round(v) for k, v in seconds.items()},
            "total_s": round(sum(seconds.values())),
            "capped": capped,
        }
        # 聞いただけの文の bonus の問い: 出した数と言えた数 (/lesson-week で見る). なければ載せない
        asked = [n for n in range(len(self.question_results)) if self.entry(n).bonus]
        if asked:
            record["bonus"] = {
                "asked": len(asked),
                "said": sum(1 for n in asked if self.question_results[n] == "ok"),
            }
        return record

    @property
    def question_results(self) -> list[str]:
        return self.results[: len(self.keys)]

    @property
    def scene_results(self) -> list[str]:
        return self.results[len(self.keys) : len(self.keys) + len(self.scenes)]

    @property
    def card_results(self) -> list[str]:
        return self.results[len(self.keys) + len(self.scenes) :]

    def _ids(self, result: str) -> list[str]:
        ids: list[str] = []
        for n, r in enumerate(self.question_results):
            if (
                r == result and not self.entry(n).bonus
            ):  # bonus: 言えなかった・迷ったは何も起こさない (#183)
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
        scene, card = self.current_scene(), self.current_card()
        if scene is not None:
            n = len(self.scene_results) + 1
            text = scenes.render_scene(scene, n, len(self.scenes), revealed, speak)
        elif card is not None:
            n = len(self.card_results) + 1
            text = reading.render_card(card, n, len(self.cards), revealed, speak)
        else:
            e = self.entry(len(self.results))
            text = (
                (
                    f"**振り返り {len(self.results) + 1}/{len(self.keys)}**"
                    f"（レッスン{e.source_lesson}）\n{e.prompt}"
                )
                + (f"\n{BONUS_NOTE}" if e.bonus else "")
                + (f"\n答え: **{e.answer}**" if revealed else "")
            )
        if not self.results:
            intro = REVIEW_INTRO.format(n=len(self.keys))
            if self.scenes and self.cards:
                intro += (
                    SCENE_INTRO.format(n=len(self.scenes))
                    + "、"
                    + READING_INTRO.format(n=len(self.cards)).removeprefix("続けて")
                )
            elif self.scenes:
                intro += (
                    SCENE_INTRO.format(n=len(self.scenes))
                    + "あります（場面の中で声に出して答える）。"
                )
            elif self.cards:
                intro += READING_INTRO.format(n=len(self.cards))
            text = intro + "\n\n" + text
        return text

    def summary(self) -> str:
        answered = self.question_results
        lines = [f"**振り返り**: {len(answered)}/{len(self.keys)}問に回答"]
        for result in ("failed", "shaky"):
            answers = [
                self.entry(n).answer
                for n, r in enumerate(answered)
                if r == result and not self.entry(n).bonus
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
        for label, done, total in (
            ("場面", self.scene_results, len(self.scenes)),
            ("読み", self.card_results, len(self.cards)),
        ):
            if done:
                counts = "・".join(
                    f"{RESULTS[r]} {done.count(r)}"
                    for r in ("ok", "shaky", "failed")
                    if r in done
                )
                lines.append(f"**{label}**: {len(done)}/{total}枚（{counts}）")
        return "\n".join(lines)


def log_review(user_dir: Path, record: dict) -> None:
    """振り返りの所要時間を review_log.jsonl に 1 行足す. 書けなくても振り返りは止めない."""
    if not record.get("answered"):
        return
    try:
        with (user_dir / REVIEW_LOG).open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        logging.warning(
            f"振り返りの所要時間を記録できませんでした ({type(e).__name__})"
        )


def read_review_log(user_dir: Path) -> list[dict]:
    out: list[dict] = []
    try:
        lines = (user_dir / REVIEW_LOG).read_text("utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def review_note(queue: ReviewQueue, day: date, limit: int) -> str:
    """投稿に添える振り返りの見通し. ``day`` は次に振り返る日."""
    pending = queue.due_count(day)
    if not pending:
        return ""
    # 直前のレッスンの新出は上限を超えても全部出る (ReviewQueue.select)
    asked = max(len(queue.must_answer()), min(pending, limit)) if limit > 0 else pending
    return f"Discord 振り返り: 次回 {asked}問（確認待ち {pending}件）"


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
        self.revealed = False
        self._show(revealed=False)

    def _speak_target(self) -> dict | None:
        """🔊 で読み上げるもの. 場面カードは答えの前は相手の言葉、後は答えの例.
        読みカードは答えの後だけ (先に聞くと読む練習にならない). 問いにはない."""
        if self.speak is None:
            return None
        scene = self.session.current_scene()
        if scene is not None:
            return scenes.speak_target(scene, self.revealed)
        card = self.session.current_card()
        return card if card is not None and self.revealed else None

    def _show(self, revealed: bool) -> None:
        """答えの前は「答えを見る」、答えの後は評価ボタン. 読み上げるものがあれば 🔊 も."""
        self.clear_items()
        self.revealed = revealed
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
        if self._speak_target() is not None:
            listen: discord.ui.Button = discord.ui.Button(
                label="🔊", style=discord.ButtonStyle.secondary
            )
            listen.callback = self._speak  # type: ignore[method-assign]
            self.add_item(listen)

    @answers_on_failure(SHOW_FAILED)
    async def _reveal(self, interaction: discord.Interaction) -> None:
        if self.is_finished() or self.session.done:
            return
        self._show(revealed=True)
        await interaction.response.edit_message(
            content=self.session.render(revealed=True, speak=self.speak is not None),
            view=self,
        )

    @answers_on_failure(SHOW_FAILED)
    async def _speak(self, interaction: discord.Interaction) -> None:
        """今のカードの音声を、押した本人にだけ mp3 で送る. 振り返りは進めない."""
        card = self._speak_target()
        if self.is_finished() or card is None or self.speak is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            async with self.speak(card) as mp3:
                await interaction.followup.send(
                    file=discord.File(mp3, filename="reading.mp3"), ephemeral=True
                )
        except Exception as e:
            logging.warning(f"カードの音声を作れませんでした ({type(e).__name__})")
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
        @answers_on_failure(RATE_FAILED)
        async def callback(interaction: discord.Interaction) -> None:
            if self.is_finished():
                return
            # 記録 (失敗なら RATE_FAILED: ボタンはそのまま押し直せる)
            self.session.rate(result)
            # ここから先は画面だけ: 失敗しても評価は記録済み
            if not self.session.done:
                self._show(revealed=False)
                content = self.session.render(speak=self.speak is not None)

                async def again() -> None:
                    # 古いメッセージのボタンはもう効かない: 今の問いを新しいメッセージで出し直し、続きはそこから
                    self.message = await interaction.followup.send(  # type: ignore[assignment]
                        content=content, view=self, wait=True
                    )

                await screen_step(
                    lambda: interaction.response.edit_message(
                        content=content, view=self
                    ),
                    again,
                )
                return
            self.stop()
            summary = self.session.summary() + "\n\n次のレッスンを生成しています…"
            await screen_step(
                lambda: interaction.response.edit_message(content=summary, view=None),
                lambda: interaction.followup.send(summary),
            )
            await self.finish(True)  # 画面の更新が失敗しても、報告と生成は止めない

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
