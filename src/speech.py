"""カードの 🔊: 学習言語の声で読み上げた mp3 (edge-tts).

「Það · Þetta」のように「·」で並べた表現は 1 つずつ読み、あいだに無音を入れる.
"""

from __future__ import annotations

import asyncio
import tempfile
import tomllib
from pathlib import Path
from typing import Awaitable, Callable


def profile_voice(profile: Path, default: str = "is-IS-GudrunNeural") -> str:
    """音声プロフィールの学習言語の声 (speakers.native_a). 読めなければ ``default``."""
    try:
        with profile.open("rb") as f:
            raw = tomllib.load(f)
        voice = raw["speakers"]["native_a"]["voice"]
    except (OSError, ValueError, KeyError, TypeError):
        return default
    return voice if isinstance(voice, str) and voice else default


# 「Það · Þetta · Því miður」のように並べた表現のあいだの無音 (秒)
PAUSE_S = 1.0
TTS = Callable[[str, str, Path], Awaitable[None]]


def expressions(text: str) -> list[str]:
    """カードに「·」で並べた表現を 1 つずつ."""
    return [t.strip() for t in text.split("·") if t.strip()]


async def edge_tts_save(text: str, voice: str, out: Path) -> None:
    """edge-tts で ``text`` を mp3 にする (ラズパイの bot の venv に入っている)."""
    import edge_tts

    await edge_tts.Communicate(text, voice, rate="-10%").save(str(out))


async def synthesize(
    text: str, voice: str, out: Path, tts: TTS | None = None, pause: float = PAUSE_S
) -> None:
    """カードの 🔊 の mp3. 「·」で並べた表現は 1 つずつ読み上げ、あいだに ``pause`` 秒の
    無音を入れてつなぐ (ffmpeg). 1 回で読ませると続けて読んでしまい、区切りが聞こえない.
    ffmpeg が使えなければ「. 」でつないで 1 回で読む (文の区切りの短い間になる)."""
    tts = tts or edge_tts_save
    words = expressions(text)
    if len(words) <= 1:
        await tts(text, voice, out)
        return
    with tempfile.TemporaryDirectory() as td:
        clips = []
        for n, word in enumerate(words):
            clip = Path(td) / f"{n}.mp3"
            await tts(word, voice, clip)
            clips.append(clip)
        if await join_with_silence(clips, pause, out):
            return
    await tts(". ".join(w.rstrip(".") for w in words) + ".", voice, out)


def join_args(clips: list[Path], pause: float, out: Path) -> list[str]:
    """clips を ``pause`` 秒の無音を挟んでつなぐ ffmpeg の引数 (edge-tts は 24kHz モノラル)."""
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for n, clip in enumerate(clips):
        if n:
            args += [
                "-f",
                "lavfi",
                "-t",
                f"{pause:g}",
                "-i",
                "anullsrc=r=24000:cl=mono",
            ]
        args += ["-i", str(clip)]
    inputs = 2 * len(clips) - 1
    graph = "".join(
        f"[{i}:a]aformat=sample_rates=24000:channel_layouts=mono[a{i}];"
        for i in range(inputs)
    )
    graph += "".join(f"[a{i}]" for i in range(inputs))
    graph += f"concat=n={inputs}:v=0:a=1[out]"
    return args + [
        "-filter_complex", graph, "-map", "[out]",
        "-codec:a", "libmp3lame", "-b:a", "48k", str(out),
    ]  # fmt: skip


async def join_with_silence(clips: list[Path], pause: float, out: Path) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            *join_args(clips, pause, out),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:  # ffmpeg がない
        return False
    return await proc.wait() == 0 and out.exists()
