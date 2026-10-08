"""Discord の操作 (ボタン・コマンド) が、最初の応答の前に例外で落ちても、学習者に何か返す.

最初の応答がないまま例外になると、Discord は「アプリケーションが応答しませんでした」と出すだけで、
学習者には原因も、記録が残っているかどうかも分からない (#78)."""

from __future__ import annotations

import functools
import logging
from typing import Any, Awaitable, Callable, TypeVar

import discord

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])

# 失敗の通知は、その場で本当のことだけを言う: 記録に触れない操作 (フォームを開く・表示する) と、
# 記録を書く操作 (評価・送信) では言えることが違う (#80 review).
FAILED = "操作を完了できませんでした（レッスンの記録は残っています）。もう一度押すか、コマンドをもう一度使ってください。"
SHOW_FAILED = "表示できませんでした。もう一度押してください。"
RATE_FAILED = (
    "この問いの評価を記録できなかった可能性があります。もう一度押してください。"
)
SEND_FAILED = "フィードバックを記録できませんでした。もう一度「送信」を押してください。"
CHOOSE_FAILED = "選択を受け付けられませんでした。もう一度選んでください。"


def _interaction(args: tuple, kwargs: dict) -> discord.Interaction | None:
    # テスト用の代役 (SimpleNamespace) も受けるので、型ではなく response を持つかで見る. 包んでいるメソッドの
    # self (View, Feedback) は response を持たない: View に response という属性を足さないこと.
    for a in (*args, *kwargs.values()):
        if isinstance(a, discord.Interaction) or hasattr(a, "response"):
            return a
    return None


def answers_on_failure(message: str = FAILED) -> Callable[[F], F]:
    """例外を記録し、まだ応答していなければ ephemeral で message を返す. 応答済みなら followup で伝える.
    例外は飲み込む (握りつぶしではなく、ログに残して学習者に返した)."""

    def wrap(fn: F) -> F:
        @functools.wraps(fn)
        async def run(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except Exception:
                logging.exception(f"{fn.__qualname__} が失敗しました")
                interaction = _interaction(args, kwargs)
                if interaction is None:
                    return None
                try:
                    if interaction.response.is_done():
                        await interaction.followup.send(message, ephemeral=True)
                    else:
                        await interaction.response.send_message(message, ephemeral=True)
                except Exception:
                    logging.exception("失敗の通知も送れませんでした")
                return None

        return run  # type: ignore[return-value]

    return wrap
