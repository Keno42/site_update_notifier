"""Discord 振り返りの読みカード (language-learning-audio #133).

音声レッスンは綴りを見せないので、振り返りの最後に数枚、看板・店の言葉・地名などを
声に出して読んでもらう. デッキは ``audiolesson reading`` の JSON をその場で読み、
ディスクには書かない: 旅程のプロフィール (trip.toml) にある自分の地名のカードが
入ることがあるため. 保存するのはカード ID ごとの予定 (reading_queue.json) だけで、
自分の地名のカードの ID は own_1, own_2 … と番号だけ.

読みカードの結果は音声レッスン側 (learner.json) には報告しない.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .review_queue import INTERVALS, next_interval

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
class ReadingQueue:
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

    def record(self, card_id: str, result: str, today: date) -> None:
        if result not in INTERVALS:
            raise ValueError(result)
        s = self.cards.setdefault(card_id, CardState())
        s.streak = s.streak + 1 if s.state == result else 1
        s.state = result
        s.reviews += 1
        s.last_reviewed = today.isoformat()
        s.due = (today + timedelta(days=next_interval(result, s.streak))).isoformat()

    @classmethod
    def load(cls, path: Path) -> "ReadingQueue":
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text("utf-8"))
            if raw.get("format") != FORMAT:
                raise ValueError(f"unknown format {raw.get('format')!r}")
            return cls({k: CardState(**v) for k, v in raw.get("cards", {}).items()})
        except (OSError, ValueError, TypeError, AttributeError) as e:
            broken = path.with_name(path.name + ".broken")
            logging.error(f"読みカードの予定を読めませんでした ({broken} に退避): {e}")
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


def parse_deck(out: str) -> list[dict]:
    """``audiolesson reading`` の出力. id と text のないカードは捨てる."""
    deck = json.loads(out)
    if not isinstance(deck, list):
        raise ValueError("the reading deck is not a list")
    return [c for c in deck if isinstance(c, dict) and c.get("id") and c.get("text")]


def render_card(card: dict, n: int, total: int, revealed: bool, speak: bool) -> str:
    """読みカード 1 枚. 意味と読み方は ``revealed`` のときだけ."""
    text = (
        f"**読み {n}/{total}**（声に出して読んでから「答えを見る」）\n"
        f"## {card['text']}"
    )
    if not revealed:
        return text
    meaning = card.get("meaning_ja") or card.get("meaning", "")
    if card.get("meaning_ja") and card.get("meaning"):
        meaning += f"（{card['meaning']}）"
    lines = [f"意味: {meaning}"]
    if card.get("hint_ja"):
        lines.append(f"読み方の目安: {card['hint_ja']}")
    parts = [p for p in card.get("parts") or [] if len(p) == 2]
    if parts:
        lines.append("成り立ち: " + " + ".join(f"{p}（{g}）" for p, g in parts))
    if speak:
        lines.append("🔊 で発音を確かめられます（本人にだけ届きます）")
    return text + "\n" + "\n".join(lines)


def profile_voice(profile: Path, default: str = "is-IS-GudrunNeural") -> str:
    """音声プロフィールの学習言語の声 (speakers.native_a). 読めなければ ``default``."""
    import tomllib

    try:
        with profile.open("rb") as f:
            raw = tomllib.load(f)
        voice = raw["speakers"]["native_a"]["voice"]
    except (OSError, ValueError, KeyError, TypeError):
        return default
    return voice if isinstance(voice, str) and voice else default


async def synthesize(text: str, voice: str, out: Path) -> None:
    """edge-tts で ``text`` を mp3 にする (ラズパイの bot の venv に入っている)."""
    import edge_tts

    await edge_tts.Communicate(text, voice, rate="-10%").save(str(out))
