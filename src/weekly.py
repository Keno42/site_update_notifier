"""週の振り返りのレポート (language-learning-audio docs/LEARNING-DESIGN.md §1, §6).

信号を並べて見るためのもの: 判断は人が、同じ画面の数字を、直近に入れた変更の予測と
突き合わせて行う (残す・戻す・もう少し待つ). 新しい記録は作らず、bot がすでに持っている
データだけを読む:

    learner.json                    レッスンごとの新出・ペース・長さ、項目ごとの結果 (history)
    lesson_manifests/*/             どの版 (language-learning-audio) と引数で生成したか、plan
    lesson_feedback.jsonl           フィードバックのフォーム
    場面カードの準備状況 (src/scenes.py) は呼び出し側が別のメッセージで添える

言えた / 迷った / 言えなかった のうち、この学習者は「迷った」をほぼ使わない (すぐ言い切って
当たるか外れるか, #129) ので、数字は 言えた と 言えなかった を中心に見る.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .feedback import FRICTIONS, LOADS, MANIFESTS, Ledger

MESSAGE_MAX = 1900
LONG_PHRASE_WORDS = 3  # 3 語以上の表現が失敗しやすい (docs/LEARNING-DESIGN.md §5.2)
FLAGS = ("--auto", "--late-unhinted-recall", "--pause-multiplier", "--new", "--pace")
OUTCOME_LABEL = {
    "recalled": "言えた",
    "hesitated": "迷った",
    "not_recalled": "言えなかった",
}


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def in_window(day: str | None, today: date, days: int) -> bool:
    """day (YYYY-MM-DD か ISO の日時) が today の days 日前から今日まで."""
    try:
        d = date.fromisoformat((day or "")[:10])
    except ValueError:
        return False
    return today - timedelta(days=days - 1) <= d <= today


def item_outcome(item: dict, lesson: int) -> str:
    """そのレッスンで出た項目の、確かめた結果. 確かめていなければ "unreported"
    (音声レッスン側では言えた扱い、docs/LEARNING-DESIGN.md H2)."""
    for h in item.get("history", []):
        if h.get("lesson") == lesson:
            if h.get("outcome"):
                return h["outcome"]
            return "not_recalled" if h.get("ok") is False else "unreported"
    return "unreported"


def word_count(target: str) -> int:
    return len([w for w in re.split(r"\s+", target) if re.search(r"\w", w)])


def targets(user_dir: Path) -> dict[str, str]:
    """項目 ID → アイスランド語 (レッスンの記録の plan.json から. 記録のないレッスンの項目は無い)."""
    out: dict[str, str] = {}
    base = user_dir / MANIFESTS
    if not base.exists():
        return out
    for d in sorted(base.iterdir()):
        for plan in d.glob("lesson-*.plan.json"):
            data = read_json(plan) or {}
            for i in data.get("new_items", []) + data.get("reviewed_items", []):
                if i.get("id") and i.get("target"):
                    out[i["id"]] = i["target"]
    return out


def manifests(user_dir: Path, today: date, days: int) -> list[dict]:
    base = user_dir / MANIFESTS
    found = []
    if base.exists():
        for d in sorted(base.iterdir()):
            m = read_json(d / "manifest.json")
            if isinstance(m, dict) and in_window(m.get("created_at"), today, days):
                found.append(m)
    return found


def lever_flags(args: list[str]) -> str:
    """生成の引数のうち、挙動を変えるもの (パスは含めない)."""
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a in FLAGS:
            takes = a in ("--pause-multiplier", "--new", "--pace") and i + 1 < len(args)
            out.append(f"{a} {args[i + 1]}" if takes else a)
            i += 2 if takes else 1
        else:
            i += 1
    return " ".join(out) or "なし"


def build(user_dir: Path, today: date, days: int = 7) -> str:
    learner = read_json(user_dir / "learner.json")
    if not isinstance(learner, dict) or not learner.get("lessons"):
        return "まだレッスンの記録がないので、週のレポートは作れません。"
    items: dict[str, dict] = learner.get("items", {})
    lessons = [
        lesson
        for lesson in learner["lessons"]
        if in_window(lesson.get("date"), today, days)
    ]
    names = targets(user_dir)
    lines = [f"📊 **直近 {days} 日のまとめ**（{today.isoformat()} まで）"]

    # 1. 量と長さ
    if lessons:
        first, last = lessons[0]["number"], lessons[-1]["number"]
        lines.append(f"**レッスン {first}〜{last}（{len(lessons)}回）**")
        for lesson in lessons:
            new = lesson.get("new_items", [])
            lines.append(
                f"・{lesson['number']}: 新出 {len(new)}（ペース {lesson.get('pace', '?')}）"
                f"、約 {lesson.get('duration_s', 0) / 60:.0f} 分"
            )
    else:
        lines.append("この期間のレッスンはありません。")

    # 2. 新出の翌日の確認
    answered: Counter[str] = Counter()
    per_lesson = []
    for lesson in lessons:
        c: Counter[str] = Counter(
            item_outcome(items.get(i, {}), lesson["number"])
            for i in lesson.get("new_items", [])
        )
        answered.update(c)
        per_lesson.append((lesson["number"], c))
    total = sum(answered.values())
    if total:
        parts = "・".join(
            f"{OUTCOME_LABEL.get(k, '未確認')} {answered[k]}"
            for k in ("recalled", "hesitated", "not_recalled", "unreported")
            if answered[k]
        )
        lines.append(f"**新出の翌日の確認**: {total} 個のうち {parts}")
        for n, c in per_lesson:
            if c["not_recalled"] or c["hesitated"]:
                missed = [
                    names.get(i, i)
                    for lesson in lessons
                    if lesson["number"] == n
                    for i in lesson.get("new_items", [])
                    if item_outcome(items.get(i, {}), n)
                    in ("not_recalled", "hesitated")
                ]
                lines.append(f"　レッスン {n}: {'、'.join(missed)}")

    # 3. 長い表現ほど失敗しやすいか (記録のあるレッスンの新出すべて)
    rows: dict[str, Counter[str]] = {"short": Counter(), "long": Counter()}
    for lesson in learner["lessons"]:
        for i in lesson.get("new_items", []):
            target = names.get(i)
            if target is None:
                continue
            outcome = item_outcome(items.get(i, {}), lesson["number"])
            if outcome == "unreported":
                continue
            kind = "long" if word_count(target) >= LONG_PHRASE_WORDS else "short"
            rows[kind]["answered"] += 1
            rows[kind]["failed"] += outcome == "not_recalled"
    if rows["short"]["answered"] and rows["long"]["answered"]:
        lines.append(
            "**言えなかった率（新出のうち、表現が記録に残っているものだけの目安）**: "
            f"1〜2語 {rows['short']['failed']}/{rows['short']['answered']}、"
            f"{LONG_PHRASE_WORDS}語以上 {rows['long']['failed']}/{rows['long']['answered']}"
        )

    # 4. フィードバックのフォーム
    events = [
        e for e in Ledger(user_dir).events() if in_window(e.get("ts"), today, days)
    ]
    if events:
        load = Counter(LOADS.get(e.get("load") or "", "未回答") for e in events)
        fric = Counter(
            FRICTIONS.get(f, f) for e in events for f in e.get("friction", [])
        )
        lines.append(
            f"**フィードバック {len(events)} 件**: 負荷 "
            + "・".join(f"{k} {v}" for k, v in load.items())
            + (
                "、気になった点 " + "・".join(f"{k} {v}" for k, v in fric.items())
                if fric
                else ""
            )
        )
        for e in events:
            if e.get("note"):
                note = e["note"].replace("\n", " ")
                lines.append(f"　メモ（レッスン {e.get('lesson')}）: {note[:80]}")

    # 5. 生成に使った版と引数 (変化があったときだけ、いつから)
    seen: list[tuple[str, str, int]] = []
    for m in manifests(user_dir, today, days):
        lla = ((m.get("revisions") or {}).get("lla") or "?")[:7]
        flags = lever_flags(m.get("generate_args", []))
        if not seen or seen[-1][:2] != (lla, flags):
            seen.append((lla, flags, int(m.get("lesson", 0))))
    if seen:
        lines.append(
            "**生成に使った版・引数**: "
            + " → ".join(
                f"レッスン {n}〜: LLA `{lla}`、引数 {flags}" for lla, flags, n in seen
            )
        )

    lines.append(
        "数字は診断のためのものです。残す・戻す・待つの判断は、直近の変更の予測と突き合わせて"
        "（language-learning-audio の docs/LEARNING-DESIGN.md §1「週のたびに」）。"
    )
    text = "\n".join(lines)
    if len(text) > MESSAGE_MAX:
        text = text[: MESSAGE_MAX - 1] + "…"
    return text


def today_local() -> date:
    return datetime.now().astimezone().date()
