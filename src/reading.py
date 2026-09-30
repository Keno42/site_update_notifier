"""Discord 振り返りの読みカード (language-learning-audio #133).

音声レッスンは綴りを見せないので、振り返りの最後に数枚、看板・店の言葉・地名などを
声に出して読んでもらう. デッキは ``audiolesson reading`` の JSON をその場で読み、
ディスクには書かない: 旅程のプロフィールにある自分の地名のカードが入ることがあるため.
予定はカード ID ごとに reading_queue.json (src/cards.py) に残す. 自分の地名のカードの
ID は own_1, own_2 … と番号だけ.
"""

from __future__ import annotations

import json


def parse_deck(out: str) -> list[dict]:
    """``audiolesson reading`` の出力. id と text のないカードは捨てる."""
    deck = json.loads(out)
    if not isinstance(deck, list):
        raise ValueError("the reading deck is not a list")
    return [c for c in deck if isinstance(c, dict) and c.get("id") and c.get("text")]


def render_card(card: dict, n: int, total: int, revealed: bool, speak: bool) -> str:
    """読みカード 1 枚. 意味と読み方は ``revealed`` のときだけ.

    文字と音のカード (letters) の meaning は綴りのきまりなので「読み方のきまり」として出し、
    並べた単語の意味は words (language-learning-audio のデッキ) から出す."""
    text = (
        f"**読み {n}/{total}**（声に出して読んでから「答えを見る」）\n"
        f"## {card['text']}"
    )
    if not revealed:
        return text
    words = [w for w in card.get("words") or [] if len(w) == 2]
    glosses = " ／ ".join(f"{w}＝{g}" for w, g in words)
    lines = []
    if card.get("stage") == "letters":
        if glosses:
            lines.append(f"意味: {glosses}")
        rule = card.get("meaning_ja") or card.get("meaning", "")
        lines.append(f"読み方のきまり: {rule}")
    else:
        meaning = card.get("meaning_ja") or card.get("meaning", "")
        if card.get("meaning_ja") and card.get("meaning"):
            meaning += f"（{card['meaning']}）"
        lines.append(f"意味: {meaning}")
        if glosses:
            lines.append(f"それぞれ: {glosses}")
    if card.get("hint_ja"):
        lines.append(f"読み方の目安: {card['hint_ja']}")
    parts = [p for p in card.get("parts") or [] if len(p) == 2]
    if parts:
        lines.append("成り立ち: " + " + ".join(f"{p}（{g}）" for p, g in parts))
    if speak:
        lines.append("🔊 で発音を確かめられます（本人にだけ届きます）")
    return text + "\n" + "\n".join(lines)
