"""/version: 動いている bot がいつ起動し、どのコミットで動いているか.

起動時に一度だけ記録する。update_and_restart.sh が pull してから再起動するまでの間や、
pull したのに再起動していないときも、ディスク上ではなく「今動いている」版を答えるため。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands

REPO_DIR = Path(__file__).resolve().parent.parent
LLA_DIR = REPO_DIR / "external" / "language-learning-audio"
SUBJECT_MAX = 72


@dataclass
class Commit:
    short: str
    date: str  # コミット日時 (サーバーのローカル時刻, 分まで)
    subject: str
    full: str = ""  # 完全なハッシュ (レッスンの記録に残す)

    def line(self, name: str) -> str:
        subject = self.subject
        if len(subject) > SUBJECT_MAX:
            subject = subject[: SUBJECT_MAX - 1] + "…"
        return f"**{name}** `{self.short}`（{self.date}）{subject}"


def head_commit(path: Path) -> Commit | None:
    """path のリポジトリ（submodule も可）の HEAD. git がない・リポジトリでないなら None."""
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "log",
                "-1",
                "--format=%h%x00%cd%x00%s%x00%H",
                "--date=format-local:%Y-%m-%d %H:%M",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    parts = out.split("\x00")
    if len(parts) != 4:
        return None
    return Commit(*parts)


def uptime(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 60 * 24)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}日{hours}時間"
    if hours:
        return f"{hours}時間{minutes}分"
    return f"{minutes}分"


@dataclass
class VersionInfo:
    started: datetime
    bot: Commit | None
    lla: Commit | None

    @classmethod
    def capture(cls) -> "VersionInfo":
        return cls(
            started=datetime.now().astimezone(),
            bot=head_commit(REPO_DIR),
            lla=head_commit(LLA_DIR),
        )

    def text(self, now: datetime | None = None) -> str:
        now = now or datetime.now().astimezone()
        running = uptime((now - self.started).total_seconds())
        lines = [
            f"起動: {self.started:%Y-%m-%d %H:%M:%S %z}（稼働 {running}）",
            (
                self.bot.line("bot")
                if self.bot
                else "**bot** 不明（git で取得できません）"
            ),
            (
                self.lla.line("language-learning-audio")
                if self.lla
                else "**language-learning-audio** 不明（submodule が未取得？）"
            ),
        ]
        return "\n".join(lines)


INFO = VersionInfo.capture()


def command() -> app_commands.Command:
    async def version(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(INFO.text(), ephemeral=True)

    return app_commands.Command(
        name="version",
        description="bot の起動日時と、動いているコミットを表示します",
        callback=version,
    )
