"""チャンネルのトピックに書く設定の節.

トピックには ``[name]`` の行から空行 (か次の ``[…]`` の行) までか、1 行の
``name = { … }`` で書く. 旅程のプロフィール (``[trip]``, src/trip.py) とレバー
(``[levers]``, src/levers.py) がこれを使う. コードブロックの ``` の行は無視する.

エラーメッセージにはキー名と位置だけを出し、値は出さない (旅程の地名などが入るため).
"""

from __future__ import annotations

import re
import tomllib
from typing import Any

_OTHER_HEADER = re.compile(r"^\s*\[[^\[\]\"]+\]\s*$")


class TopicError(ValueError):
    """節を読めない. メッセージに値は入れない (キー名と位置だけ)."""


def channel_topic(channel: Any) -> str:
    """チャンネルのトピック. スレッドなら親チャンネルのもの."""
    topic = getattr(channel, "topic", None)
    parent = getattr(channel, "parent", None)
    if topic is None and parent is not None:
        topic = getattr(parent, "topic", None)
    return topic if isinstance(topic, str) else ""


def section(topic: str, name: str) -> dict | None:
    """トピックの ``name`` の節 (TOML の表). 書かれていなければ None."""
    header = re.compile(rf"^\s*\[\s*{re.escape(name)}\s*\]\s*$", re.IGNORECASE)
    inline = re.compile(rf"^\s*{re.escape(name)}\s*=\s*\{{.*\}}\s*$", re.IGNORECASE)
    lines = [ln for ln in topic.splitlines() if not ln.strip().startswith("```")]
    for i, line in enumerate(lines):
        if inline.match(line):
            raw = _loads(line.strip()).get(name)
            if not isinstance(raw, dict):
                raise TopicError(f"{name} = {{ … }} の形で書いてください")
            return raw
        if header.match(line):
            body = []
            for ln in lines[i + 1 :]:
                if not ln.strip() or _OTHER_HEADER.match(ln):
                    break
                body.append(ln)
            return _loads("\n".join(body))
    return None


def _loads(text: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        # tomllib のメッセージは位置だけで、値は含まない
        raise TopicError(f"TOML として読めません（{e}）") from None
