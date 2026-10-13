"""カードの 🔊: 学習言語の声で読み上げた mp3 (edge-tts).

「Það · Þetta」のように「·」で並べた表現は 1 つずつ読み、あいだに無音を入れる.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import tomllib
from pathlib import Path
from typing import Awaitable, Callable

from . import secrets_box


def profile_voice(profile: Path, default: str = "is-IS-GudrunNeural") -> str:
    """音声プロフィールの学習言語の声 (speakers.native_a). 読めなければ ``default``."""
    try:
        with profile.open("rb") as f:
            raw = tomllib.load(f)
        voice = raw["speakers"]["native_a"]["voice"]
    except (OSError, ValueError, KeyError, TypeError):
        return default
    return voice if isinstance(voice, str) and voice else default


# 「Það · Þetta · Því miður」のように並べた表現のあいだの無音 (秒). 各クリップの前後の無音は切り落とす
# (``TRIM``) ので、聞こえる間はこの値そのもの. 区切りが聞こえて、人が読み上げる並びに聞こえる長さ
PAUSE_S = 0.5
# 先頭の無音を落とし、反転して先頭 (= 元の末尾) の無音を落とし、元に戻す (edge-tts のクリップは前後に数十〜数百 ms の無音を持つ)
_TRIM = "silenceremove=start_periods=1:start_threshold=-50dB"
TRIM = f"{_TRIM},areverse,{_TRIM},areverse"
TTS = Callable[[str, str, Path], Awaitable[None]]


def expressions(text: str) -> list[str]:
    """カードに「·」で並べた表現を 1 つずつ."""
    return [t.strip() for t in text.split("·") if t.strip()]


async def edge_tts_save(text: str, voice: str, out: Path) -> None:
    """edge-tts で ``text`` を mp3 にする (ラズパイの bot の venv に入っている)."""
    import edge_tts

    await edge_tts.Communicate(text, voice, rate="-10%").save(str(out))


async def azure_tts_save(text: str, voice: str, out: Path) -> None:
    """Azure Speech の公式 REST API で ``text`` を mp3 にする (language-learning-audio の ``AzureProvider`` と同じ声・同じ呼び方. issue #119).
    鍵は secrets_box (メモリ) から、ヘッダにだけ載せる: ログにも URL にもエラーメッセージにも出さない."""
    import aiohttp
    from xml.sax.saxutils import escape

    key, region = secrets_box.SECRETS["AZURE_SPEECH_KEY"], secrets_box.SECRETS["AZURE_SPEECH_REGION"]
    locale = "-".join(voice.split("-")[:2]) or "is-IS"
    ssml = (
        f"<speak version='1.0' xml:lang='{locale}'><voice name='{escape(voice)}'>"
        f"<prosody rate='-10%'>{escape(text)}</prosody></voice></speak>"
    )
    url = os.environ.get("AZURE_SPEECH_ENDPOINT") or f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
        "User-Agent": "site_update_notifier",
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        async with session.post(url, data=ssml.encode("utf-8"), headers=headers) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Azure Speech failed (HTTP {resp.status})")  # 鍵は含めない
            out.write_bytes(await resp.read())


def default_tts() -> TTS:
    """封じた secrets で Azure の鍵があれば Azure (edge-tts が断られていても読める), なければ edge-tts."""
    if all(secrets_box.SECRETS.get(k) for k in secrets_box.ALLOWLIST):
        return azure_tts_save
    return edge_tts_save


async def synthesize(
    text: str, voice: str, out: Path, tts: TTS | None = None, pause: float = PAUSE_S
) -> None:
    """カードの 🔊 の mp3. 「·」で並べた表現は 1 つずつ読み上げ、あいだに ``pause`` 秒の
    無音を入れてつなぐ (ffmpeg). 1 回で読ませると続けて読んでしまい、区切りが聞こえない.
    ffmpeg が使えなければ「. 」でつないで 1 回で読む (文の区切りの短い間になる)."""
    tts = tts or default_tts()
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
    """clips を ``pause`` 秒の無音を挟んでつなぐ ffmpeg の引数 (edge-tts は 24kHz モノラル). 各クリップは前後の無音を切り落としてからつなぐ."""
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
        f"[{i}:a]aformat=sample_rates=24000:channel_layouts=mono"
        + (f",{TRIM}" if i % 2 == 0 else "")  # 偶数番が音声クリップ、奇数番が挟む無音
        + f"[a{i}];"
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
