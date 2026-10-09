"""ユーザーごとの既定 (/lesson-configure): レッスンの長さ、新出表現の一覧を出すタイミング、並び順.

希望するユーザー向けのオプションで、学習設計の外にある (今の設定のままのユーザーには何も変わらない).
user_dir/lesson_settings.json に置く (language-learning-audio の --user が使う settings.json とは別の名前).
/lesson の minutes は 1 回だけの上書きで、ここには書かない.

振り返り (問い・カード・必須にする未解決項目の確認) とフィードバックの量は、レッスンの長さに比例させる
(BASE_MINUTES で config の値そのまま)."""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path

SETTINGS_FILE = "lesson_settings.json"
BASE_MINUTES = 30
MINUTES_CHOICES = (5, 10, 15, 30)
NEW_LIST = {"before": "レッスンと一緒に出す", "after": "フィードバックの後に出す"}
ORDER = {
    "new-first": "新出をまとめて先に → 既出",
    "spread": "新出と既出を混ぜる（今まで）",
}
# これより短いレッスンのフィードバックは「量・難しさ」「練習が足りなかった」と送信だけ
COMPACT_FEEDBACK_BELOW = 15
# 必須にする未解決項目の確認の、BASE_MINUTES での目安 (短いレッスンだけに使う上限)
OPEN_CHECKS_AT_BASE = 6


@dataclass
class UserSettings:
    minutes: float
    new_list: str  # NEW_LIST のキー
    order: str  # ORDER のキー

    def describe(self) -> str:
        return (
            f"レッスンの長さ: {self.minutes:g}分 / 新出表現の一覧: {NEW_LIST[self.new_list]}"
            f" / 並び順: {ORDER[self.order]}"
        )


def load(user_dir: Path, default: UserSettings, legacy: UserSettings) -> UserSettings:
    """設定ファイルがあればそれ. なければ、learner.json がある (設定ができる前からの) ユーザーは
    legacy (今までの動作)、新しいユーザーは default. 読めない値は default に戻す."""
    path = user_dir / SETTINGS_FILE
    if not path.exists():
        base = legacy if (user_dir / "learner.json").exists() else default
        return UserSettings(**asdict(base))
    try:
        raw = json.loads(path.read_text("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as e:
        logging.warning(
            f"レッスンの設定を読めませんでした ({type(e).__name__}): 既定を使います"
        )
        return UserSettings(**asdict(default))
    return UserSettings(
        minutes=(
            raw["minutes"] if raw.get("minutes") in MINUTES_CHOICES else default.minutes
        ),
        new_list=(
            raw["new_list"] if raw.get("new_list") in NEW_LIST else default.new_list
        ),
        order=raw["order"] if raw.get("order") in ORDER else default.order,
    )


def save(user_dir: Path, settings: UserSettings) -> None:
    user_dir.mkdir(parents=True, exist_ok=True)
    path = user_dir / SETTINGS_FILE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, path)


def scaled(n: int, minutes: float, floor: int = 0) -> int:
    """BASE_MINUTES を基準に比例させた数. 0 以下 (上限なし / 出さない) はそのまま."""
    if n <= 0:
        return n
    return max(floor, math.ceil(n * minutes / BASE_MINUTES))


def open_limit(minutes: float) -> int | None:
    """必須にする未解決項目の確認の上限. BASE_MINUTES 以上は上限なし (language-learning-audio #220 のまま)."""
    if minutes >= BASE_MINUTES:
        return None
    return scaled(OPEN_CHECKS_AT_BASE, minutes, floor=1)


@dataclass(frozen=True)
class ReviewSize:
    """1 回の振り返りの量."""

    questions: int  # 問いとカードを合わせた上限 (review_limit と同じく 0 なら上限なし)
    scene_cards: int
    reading_cards: int
    open_limit: int | None  # 必須にする未解決項目の確認の上限 (None ならすべて)

    @classmethod
    def for_minutes(
        cls, minutes: float, review_limit: int, scene_cards: int, reading_cards: int
    ) -> "ReviewSize":
        return cls(
            questions=scaled(review_limit, minutes, floor=3),
            scene_cards=scaled(scene_cards, minutes),
            reading_cards=scaled(reading_cards, minutes),
            open_limit=open_limit(minutes),
        )

    @property
    def question_limit(self) -> int:
        """カードを出すときの問いの上限の目安 (LessonConfig.question_limit と同じ計算)."""
        cards = max(self.scene_cards, 0) + max(self.reading_cards, 0)
        if self.questions <= 0 or cards <= 0:
            return self.questions
        return max(self.questions - cards, 1)
