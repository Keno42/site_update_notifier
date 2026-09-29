"""旅程のプロフィール (language-learning-audio #132) の出どころ.

1. ユーザーのディレクトリの trip.toml (その人だけの設定. あればこちらを使う)
2. /lesson を実行したチャンネルのトピック (同じ旅行の人だけがいるチャンネル向け)

トピックには ``[trip]`` の節か、1 行の ``trip = { … }`` で書く:

    アイスランド旅行 🇮🇸
    [trip]
    departure = 2030-01-31
    boost = ["A6", "B2"]
    places = ["…"]
    season = "winter-holidays"

トピックの設定は決まった形の TOML に書き直して一時ファイルにし、CLI に渡したら消す.
bot が残すのは sha256 だけで、設定の値はログにもエラーメッセージにも出さない.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

KEYS = ("departure", "boost", "places", "season")
TOPIC = "channel-topic"  # レッスンの記録の引数で、一時ファイルのパスの代わりに残す
_HEADER = re.compile(r"^\s*\[\s*trip\s*\]\s*$", re.IGNORECASE)
_OTHER_HEADER = re.compile(r"^\s*\[[^\[\]\"]+\]\s*$")
_INLINE = re.compile(r"^\s*trip\s*=\s*\{.*\}\s*$", re.IGNORECASE)


class TripError(ValueError):
    """設定を読めない. メッセージに設定の値は入れない (キー名と位置だけ)."""


@dataclass
class TripSource:
    path: Path  # CLI の --trip に渡すファイル
    origin: str  # "file" / TOPIC
    sha256: str


def channel_topic(channel: Any) -> str:
    """チャンネルのトピック. スレッドなら親チャンネルのもの."""
    topic = getattr(channel, "topic", None)
    parent = getattr(channel, "parent", None)
    if topic is None and parent is not None:
        topic = getattr(parent, "topic", None)
    return topic if isinstance(topic, str) else ""


def parse_topic(topic: str) -> dict | None:
    """トピックの旅程の設定. 書かれていなければ None."""
    lines = [ln for ln in topic.splitlines() if not ln.strip().startswith("```")]
    for i, line in enumerate(lines):
        if _INLINE.match(line):
            raw = _loads(line.strip()).get("trip")
            if not isinstance(raw, dict):
                raise TripError("trip = { … } の形で書いてください")
            return validate(raw)
        if _HEADER.match(line):
            body = []
            for ln in lines[i + 1 :]:
                if not ln.strip() or _OTHER_HEADER.match(ln):
                    break
                body.append(ln)
            return validate(_loads("\n".join(body)))
    return None


def _loads(text: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        # tomllib のメッセージは位置だけで、値は含まない
        raise TripError(f"TOML として読めません（{e}）") from None


def validate(raw: dict) -> dict:
    """language-learning-audio の load_trip と同じ決まり."""
    unknown = sorted(set(raw) - set(KEYS))
    if unknown:
        raise TripError(f"知らないキー {unknown}（使えるのは {list(KEYS)}）")
    out: dict = {}
    dep = raw.get("departure")
    if isinstance(dep, datetime):
        dep = dep.date()
    elif isinstance(dep, str):
        try:
            dep = date.fromisoformat(dep)
        except ValueError:
            raise TripError("departure は日付（YYYY-MM-DD）で書いてください") from None
    if dep is not None:
        if not isinstance(dep, date):
            raise TripError("departure は日付（YYYY-MM-DD）で書いてください")
        out["departure"] = dep
    for key in ("boost", "places"):
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise TripError(f"{key} は文字列のリストで書いてください")
        if value:
            out[key] = value
    season = raw.get("season")
    if season is not None:
        if not isinstance(season, str):
            raise TripError("season は文字列で書いてください")
        out["season"] = season
    return out


def dump(trip: dict) -> str:
    """決まった形の TOML (書き方が違っても中身が同じなら同じ sha256 になる)."""
    lines = []
    for key in KEYS:
        if key not in trip:
            continue
        value = trip[key]
        if isinstance(value, date):
            text = value.isoformat()
        elif isinstance(value, list):
            text = (
                "[" + ", ".join(json.dumps(x, ensure_ascii=False) for x in value) + "]"
            )
        else:
            text = json.dumps(value, ensure_ascii=False)
        lines.append(f"{key} = {text}")
    return "".join(ln + "\n" for ln in lines)


@contextlib.contextmanager
def resolve(file: Path, channel: Any) -> Iterator[tuple[TripSource | None, str]]:
    """使う旅程の設定と、読めなかったときの案内 (なければ ""). トピックの設定は
    with の間だけ一時ファイルになる."""
    if file.exists():
        try:
            digest = hashlib.sha256(file.read_bytes()).hexdigest()
        except OSError:
            yield None, "trip.toml を読めませんでした。旅程なしで生成します。"
            return
        yield TripSource(file, "file", digest), ""
        return
    try:
        trip = parse_topic(channel_topic(channel))
    except TripError as e:
        yield None, (
            f"チャンネルのトピックの旅程の設定を読めませんでした: {e}。"
            "旅程なしで続けます。"
        )
        return
    if trip is None:
        yield None, ""
        return
    text = dump(trip).encode("utf-8")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "trip.toml"
        path.write_bytes(text)
        yield TripSource(path, TOPIC, hashlib.sha256(text).hexdigest()), ""


def recorded_args(args: list[str], source: TripSource | None) -> list[str]:
    """レッスンの記録に残す引数: トピックの一時ファイルのパスは channel-topic に."""
    if source is None or source.origin != TOPIC:
        return list(args)
    return [TOPIC if a == str(source.path) else a for a in args]
