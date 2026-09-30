"""Discord 振り返りの場面カード (language-learning-audio #129).

旅行の can-do 場面 (#131) の一場面を台本どおりに: 日本語の状況 → (あれば) 🔊 で相手の
アイスランド語を聞く → 声に出して答える →「答えを見る」で答えの例と、相手の言葉の文字と
意味. GPT の音声練習の代わり (台本どおりに進まず、書き起こしも実際と違ったため).

カードは ``audiolesson scenes`` の JSON をその場で読む. 予定は読みカードと同じ形
(CardQueue) で scene_queue.json に、カード ID ごとに残す. 結果は音声レッスン側
(learner.json) には報告しない.

週に一度、場面ごとの準備状況をレッスンの投稿に添える (readiness).
"""

from __future__ import annotations

import json

from .cards import CardQueue

KIND_HINT = {
    "respond": "🔊 で相手の言葉を聞いて、声に出して答えてから「答えを見る」",
    "repair": "🔊 で相手の言葉を聞いて（全部分からなくて大丈夫）、声に出して返してから「答えを見る」",
    "initiate": "声に出して言ってから「答えを見る」",
}


def parse_scenes(out: str) -> list[dict]:
    """``audiolesson scenes`` の出力. id と答えの例のないカードは捨てる."""
    cards = json.loads(out)
    if not isinstance(cards, list):
        raise ValueError("the scenario cards are not a list")
    return [
        c
        for c in cards
        if isinstance(c, dict) and c.get("id") and c.get("replies") and c.get("kind")
    ]


def speak_target(card: dict, revealed: bool) -> dict | None:
    """🔊 で読み上げるもの: 答えの前は相手の言葉 (あれば)、答えの後は答えの例."""
    if revealed:
        return {"text": card["replies"][0]}
    if card.get("partner"):
        return {"text": card["partner"]}
    return None


def render_scene(card: dict, n: int, total: int, revealed: bool, speak: bool) -> str:
    """場面カード 1 枚. 相手の言葉の文字と意味、答えの例は ``revealed`` のときだけ.
    speak: 🔊 ボタンがある (ないときは相手の言葉を最初から文字で出す)."""
    title = card.get("title_ja") or card.get("title") or card.get("scenario", "")
    situation = card.get("situation_ja") or card.get("situation", "")
    lines = [f"**場面 {n}/{total}**（{title}）", situation]
    partner = card.get("partner", "")
    if not revealed:
        if partner and not speak:
            lines.append(f"相手: «{partner}»")
            lines.append("声に出して答えてから「答えを見る」")
        else:
            lines.append(KIND_HINT.get(card.get("kind", ""), KIND_HINT["initiate"]))
        return "\n".join(lines)
    if partner:
        meaning = card.get("partner_meaning_ja") or card.get("partner_meaning", "")
        lines.append(f"相手: «{partner}»" + (f" — {meaning}" if meaning else ""))
    lines.append("答えの例: " + " ／ ".join(card["replies"]))
    if card.get("note_ja"):
        lines.append(f"メモ: {card['note_ja']}")
    if speak:
        lines.append("🔊 で答えの例の発音を確かめられます（本人にだけ届きます）")
    return "\n".join(lines)


def readiness(all_cards: list[dict], available: set[str], queue: CardQueue) -> str:
    """場面ごとの準備状況 (#129 の指標). 準備OK: その場面のカードが全部出題できて、
    最後の評価がどれも言えた. 未学習: まだ 1 枚も出題できない. それ以外は練習中."""
    scenarios: dict[str, dict] = {}
    for c in all_cards:
        s = scenarios.setdefault(
            c["scenario"],
            {
                "tier": c.get("tier", ""),
                "title": c.get("title_ja") or c["scenario"],
                "cards": [],
            },
        )
        s["cards"].append(c["id"])
    lines = []
    for tier in ("A", "B"):
        rows = [s for s in scenarios.values() if s["tier"] == tier]
        if not rows:
            continue
        ready, practising, untaught = [], [], []
        for s in rows:
            ids = s["cards"]
            if not any(i in available for i in ids):
                untaught.append(s["title"])
            elif all(i in available and _state(queue, i) == "ok" for i in ids):
                ready.append(s["title"])
            else:
                practising.append(s["title"])
        line = (
            f"Tier {tier}（{len(rows)}場面）: 準備OK {len(ready)}・練習中 {len(practising)}"
            f"・未学習 {len(untaught)}"
        )
        if ready:
            line += f"\n　準備OK: {'、'.join(ready)}"
        lines.append(line)
    if not lines:
        return ""
    return "📋 **旅行の準備（場面カード）**\n" + "\n".join(lines)


def _state(queue: CardQueue, card_id: str) -> str:
    s = queue.cards.get(card_id)
    return s.state if s else "unseen"
