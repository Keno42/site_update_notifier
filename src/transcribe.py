"""音声ファイルの書き起こし (OpenAI Whisper). Discord と Slack の音声添付に使う.

長い音声は約 12 分ずつ (2 秒重ねて) に分け (src/audio_utils.py)、前のチャンクの書き起こしを
次のチャンクのヒントにする.
"""

from __future__ import annotations

import logging
import tempfile
import time

from openai import OpenAI

from config import config

from .audio_utils import split_audio_with_overlap

_client: OpenAI | None = None


def _openai() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=config.CHATGPT_TOKEN, timeout=180.0)
    return _client


def transcribe_chunk(path: str, context: str) -> str:
    """1 チャンクの書き起こし. 失敗したら理由を <…> で返す (他のチャンクは続ける)."""
    try:
        started = time.time()
        with open(path, "rb") as f:
            response = _openai().audio.transcriptions.create(
                file=f, model="whisper-1", prompt=context
            )
        logging.info(f"文字起こし完了: {path}（{time.time() - started:.1f}秒）")
        return response.text
    except Exception as e:
        logging.error(f"文字起こし処理でエラーが発生: {e}")
        return f"<書き起こしに失敗しました: {e}>"


def transcribe_file(path: str, context: str = "") -> str:
    """音声ファイル全体の書き起こし (同期. イベントループからは asyncio.to_thread で)."""
    texts: list[str] = []
    with tempfile.TemporaryDirectory() as chunks_dir:
        chunks = split_audio_with_overlap(path, output_dir=chunks_dir)
        for n, chunk in enumerate(chunks, 1):
            logging.info(f"チャンク {n}/{len(chunks)} の文字起こしを開始")
            hint = f"{context}\n\n{texts[-1]}" if texts else context
            texts.append(transcribe_chunk(chunk, hint))
    return "\n".join(texts)
