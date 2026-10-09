"""新出表現の一覧は、レッスンを聞いた後に出す (#79). ユーザーの設定 (/lesson-configure, usersettings) で
「レッスンと一緒に出す」を選んだ人には投稿のすぐ後に出し、後に出す人も「新出表現をすぐ表示」で今出せる.

聞く前に綴りを読むと、新しい言葉との最初の出会いが「耳で」ではなくなる (このコースは音声が先で、
読みは別の道, language-learning-audio #133). レッスンの投稿には数だけ載せ、一覧 (表現と英語の意味)
は、フィードバックを送ったときにレッスンの投稿への返信として出す. 送らないまま次の生成が始まるなら、
その時に出す (なくさない). どちらで出したかを残すため、出していない一覧はユーザーのディレクトリに置く."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Callable

import discord

from .interaction import answers_on_failure

PENDING_FILE = "new_list_pending.json"
LIMIT = 1900  # Discord の 1 通は 2000 字まで


def unique_items(items: list[dict]) -> list[dict]:
    """同じ id は 1 つ (先に出たもの). 埋め込みで聞いた後に新出として導入された表現が 2 回載る
    ことがある. 選択肢は value が重複すると Discord が拒否する (#78)."""
    seen: set[str] = set()
    out: list[dict] = []
    for i in items:
        if i["id"] not in seen:
            seen.add(i["id"])
            out.append(i)
    return out


def count_text(plan: dict) -> str:
    """レッスンの投稿に載せる: 数だけ."""
    return f"新出 {len(unique_items(plan.get('new_items', [])))}"


def list_text(lesson: int, items: list[dict]) -> str:
    """レッスンごとの一覧: 1 行に 1 つ、表現と英語の意味."""
    lines = []
    for i in unique_items(items):
        target = i.get("target") or i["id"]
        meaning = i.get("meaning")
        lines.append(f"・{target} — {meaning}" if meaning else f"・{target}")
    text = f"**レッスン {lesson} の新出表現**\n" + "\n".join(lines)
    return text if len(text) <= LIMIT else text[: LIMIT - 1] + "…"


class PendingLists:
    """まだ出していない一覧 (レッスンの記録の ID → 投稿の場所と本文)."""

    def __init__(self, user_dir: Path) -> None:
        self.path = user_dir / PENDING_FILE

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")

    def add(
        self, record: str, channel_id: int, message_id: int, text: str, lesson: int = 0
    ) -> None:
        """一覧を置く. 同じ番号のレッスンを生成し直したときは、前の版 (lesson-019 に対する lesson-019.2) の
        一覧は捨てる: 聞くのは新しい版で、前の投稿の一覧は別の新出かもしれない (#83 review)."""
        data = self._load()
        if lesson:
            data = {k: v for k, v in data.items() if v.get("lesson") != lesson}
        data[record] = {
            "channel": channel_id,
            "message": message_id,
            "text": text,
            "lesson": lesson,
        }
        self._save(data)

    def take(self, record: str) -> dict | None:
        """その記録の一覧を取り出して、残さない (二度出さない)."""
        data = self._load()
        entry = data.pop(record, None)
        if entry is not None:
            self._save(data)
        return entry

    def take_all(self) -> list[dict]:
        data = self._load()
        if data:
            self._save({})
        return list(data.values())


async def send(channel: Any, entry: dict) -> None:
    """レッスンの投稿への返信として出す. 投稿が消えていても、ふつうの投稿として出る."""
    ref = discord.MessageReference(
        message_id=entry["message"],
        channel_id=entry["channel"],
        fail_if_not_exists=False,
    )
    await channel.send(entry["text"], reference=ref, mention_author=False)


async def release(channel: Any, entries: list[dict]) -> None:
    """一覧を出す. 出せなくても呼び出し側 (フィードバックの記録・生成) は止めない."""
    for entry in entries:
        try:
            await send(channel, entry)
        except Exception:
            logging.exception("新出表現の一覧を出せませんでした")


class NewListButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"lla-newlist:(?P<owner>[0-9]+):(?P<manifest>lesson-[0-9]+(?:\.[0-9]+)?)",
):
    """「新出表現をすぐ表示」: 一覧をフィードバックまで取っておく (after) 人が、今見たいときに出す.
    custom_id にレッスンを受けた人と記録の ID を持つので、bot を再起動した後でも押せる. 押せるのはその人だけ."""

    users: dict[int, str] = {}
    user_dir: Callable[[str], Path] | None = None

    def __init__(self, owner: int, manifest: str) -> None:
        super().__init__(
            discord.ui.Button(
                label="新出表現をすぐ表示",
                style=discord.ButtonStyle.secondary,
                custom_id=f"lla-newlist:{owner}:{manifest}",
            )
        )
        self.owner = owner
        self.manifest = manifest

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> "NewListButton":
        return cls(int(match["owner"]), match["manifest"])

    @answers_on_failure()
    async def callback(self, interaction: discord.Interaction) -> None:
        name = NewListButton.users.get(self.owner)
        if (
            interaction.user.id != self.owner
            or name is None
            or NewListButton.user_dir is None
        ):
            await interaction.response.send_message(
                "このレッスンを受けた人だけが押せます。", ephemeral=True
            )
            return
        entry = PendingLists(NewListButton.user_dir(name)).take(self.manifest)
        if entry is None:
            await interaction.response.send_message(
                "一覧はもう出ています。", ephemeral=True
            )
            return
        await interaction.response.send_message(entry["text"])
