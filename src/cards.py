"""振り返りのカード (場面カード・読みカード) の予定.

カード ID ごとに、最後の評価・同じ評価の続いた回数・次の期限だけを持つ. 次の確認までの
日数は振り返りの問いと同じ (review_queue.INTERVALS). 結果は音声レッスン側
(learner.json) には報告しない. 場面カードは scene_queue.json、読みカードは
reading_queue.json に同じ形で保存する.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .review_queue import INTERVALS, next_interval

# 読みカードが先にあったので、場面カードの予定もこの名前の形式で保存する
FORMAT = "lesson-reading-queue/1"
# 期限の来たカードの出す順: 言えなかった → 迷った → 言えた
RANK = {"failed": 0, "shaky": 1, "ok": 2}


@dataclass
class CardState:
    state: str = "unseen"
    streak: int = 0
    reviews: int = 0
    last_reviewed: str | None = None
    due: str = ""


@dataclass
class CardQueue:
    cards: dict[str, CardState] = field(default_factory=dict)

    def select(self, deck: list[dict], today: date, n: int) -> list[dict]:
        """今回出すカード (最大 n 枚): 期限の来たカードを先に、残りをデッキ順の新しい
        カードで埋める. デッキにないカード (消えたカード) は出さない."""
        if n <= 0:
            return []
        by_id = {c["id"]: c for c in deck}
        due = [
            k
            for k, s in self.cards.items()
            if k in by_id and s.due and date.fromisoformat(s.due) <= today
        ]
        due.sort(key=lambda k: (RANK.get(self.cards[k].state, 3), self.cards[k].due))
        new = [c["id"] for c in deck if c["id"] not in self.cards]
        return [by_id[k] for k in (due + new)[:n]]

    def record(self, card_id: str, result: str, today: date, skip_first_ok: bool = False) -> None:
        """``skip_first_ok``: 場面カードを初めて見て「言えた」ときは、「言えた」の最初の段 (1 日) を飛ばして 3 日後から
        始める (language-learning-audio の場面カード: 翌朝また出ると繰り返しに感じる, site_update_notifier#95). 言えなかった・迷った
        の 1 日、読みカードと項目の問いの間隔は変えない."""
        if result not in INTERVALS:
            raise ValueError(result)
        first = card_id not in self.cards
        s = self.cards.setdefault(card_id, CardState())
        s.streak = s.streak + 1 if s.state == result else 1
        if skip_first_ok and first and result == "ok":
            s.streak = 2  # the first step of the ok sequence is skipped: 3 days now, 7 after the next «ok»
        s.state = result
        s.reviews += 1
        s.last_reviewed = today.isoformat()
        s.due = (today + timedelta(days=next_interval(result, s.streak))).isoformat()

    @classmethod
    def load(cls, path: Path) -> "CardQueue":
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text("utf-8"))
            if raw.get("format") != FORMAT:
                raise ValueError(f"unknown format {raw.get('format')!r}")
            return cls({k: CardState(**v) for k, v in raw.get("cards", {}).items()})
        except (OSError, ValueError, TypeError, AttributeError) as e:
            broken = path.with_name(path.name + ".broken")
            logging.error(f"カードの予定を読めませんでした ({broken} に退避): {e}")
            path.replace(broken)
            return cls()

    def save(self, path: Path) -> None:
        data = {
            "format": FORMAT,
            "cards": {k: asdict(s) for k, s in self.cards.items()},
        }
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, path)
