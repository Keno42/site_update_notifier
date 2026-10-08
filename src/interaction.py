"""Discord の操作 (ボタン・コマンド) が、最初の応答の前に例外で落ちても、学習者に何か返す.

最初の応答がないまま例外になると、Discord は「アプリケーションが応答しませんでした」と出すだけで、
学習者には原因も、記録が残っているかどうかも分からない (#78)."""

from __future__ import annotations

import functools
import logging
from typing import Any, Awaitable, Callable, TypeVar

import discord

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])

FAILED = "操作を完了できませんでした（記録は残っています）。もう一度押すか、コマンドをもう一度使ってください。"


def _interaction(args: tuple, kwargs: dict) -> discord.Interaction | None:
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
