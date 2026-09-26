"""Discord 振り返りのキュー (issue #31).

音声レッスン側の間隔反復 (learner.json) とは別に、「いつ Discord で自己申告を
求めるか」を管理する。問いは一度入ったら消さず、答えるまで unseen のまま残る。
1 回の振り返りは上限まで、優先度の高いものから出す。

単位は項目ではなく問い: «Ég vil fara heim.» のように複数の項目を一度に言う問いは
1 件で、その結果は含まれる項目すべてに当てはめる (どちらで詰まったかは分けられない).
同じ項目が別の問いにも入ることがある.

答えた結果のうち音声レッスン側 (audiolesson report) にまだ届いていないものは
``pending_reports`` に出題元のレッスンごとに残し、報告できたら消す.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

FORMAT = "lesson-review-queue/2"
STATES = ("unseen", "ok", "shaky", "failed")

# 結果ごとの次の確認までの日数。同じ結果が続くたびに次の値へ進む (最後の値で頭打ち)
INTERVALS: dict[str, tuple[int, ...]] = {
    "failed": (1,),
    "shaky": (1, 3, 7),
    "ok": (1, 3, 7, 14, 30),
}
# これだけ期限を過ぎた問いは新出項目と同じ優先度に上げる (新出が毎回あっても埋もれない)
PROMOTE_AFTER_DAYS = 7


def next_interval(result: str, streak: int) -> int:
    """``result`` が ``streak`` 回続いたときの、次の確認までの日数."""
    seq = INTERVALS[result]
    return seq[min(max(streak, 1), len(seq)) - 1]


@dataclass
class Entry:
    items: list[str]
    prompt: str
    answer: str
    source_lesson: int
    state: str = "unseen"
    new: bool = False  # 新出項目の問いとして入った
    reviews: int = 0
    streak: int = 0  # 同じ結果が続いた回数
    last_reviewed: str | None = None
    due: str = ""

    def tier(self, today: date) -> int:
        """小さいほど先に出す. 期限前は 5."""
        due = date.fromisoformat(self.due)
        if due > today:
            return 5
        if self.state == "failed":
            return 0
        tier = {"shaky": 2, "ok": 4}.get(self.state, 1 if self.new else 3)
        if tier > 1 and (today - due).days >= PROMOTE_AFTER_DAYS:
            tier = 1  # 長く待たされている
        return tier


def key_for(items: list[str]) -> str:
    return "+".join(items)


@dataclass
class ReviewQueue:
    entries: dict[str, Entry] = field(default_factory=dict)
    # 出題元レッスン → まだ report していない「言えなかった」項目. キーがあること自体が
    # 「このレッスンの問いに答えたが、まだ報告していない」を表す (言えただけなら空リスト)
    pending_reports: dict[int, list[str]] = field(default_factory=dict)

    # ---- 選ぶ ---------------------------------------------------------

    def select(self, today: date, limit: int = 0) -> list[str]:
        """今回の振り返りで出す問いのキー. limit > 0 なら最大 limit 問で、期限の来ている
        問いが足りなければ期限前の問いで埋める. limit = 0 なら期限の来ている問いすべて."""

        def order(k: str) -> tuple:
            e = self.entries[k]
            return (e.tier(today), e.due, e.last_reviewed or "")

        ranked = sorted(self.entries, key=order)
        if limit <= 0:
            return [k for k in ranked if self.entries[k].tier(today) < 5]
        return ranked[:limit]

    def due_count(self, today: date) -> int:
        return sum(1 for e in self.entries.values() if e.tier(today) < 5)

    # ---- 更新する -------------------------------------------------------

    def record(self, key: str, result: str, today: date) -> None:
        """1 問の結果. その問いの状態と次の期限だけを変える."""
        if result not in INTERVALS:
            raise ValueError(result)
        e = self.entries[key]
        e.streak = e.streak + 1 if e.state == result else 1
        e.state = result
        e.reviews += 1
        e.last_reviewed = today.isoformat()
        e.due = (today + timedelta(days=next_interval(result, e.streak))).isoformat()
        failed = self.pending_reports.setdefault(e.source_lesson, [])
        if result == "failed":
            failed += [i for i in e.items if i not in failed]

    # ---- 音声レッスン側への報告 -------------------------------------------

    def reports(self) -> list[tuple[int, list[str]]]:
        """まだ届いていない報告: (出題元レッスン, 言えなかった項目) をレッスン順に."""
        return [(n, list(ids)) for n, ids in sorted(self.pending_reports.items())]

    def mark_reported(self, lesson: int, sent: list[str]) -> None:
        """``lesson`` の報告が届いた. 送った後に増えた分 (報告中に答えた問い) は残す."""
        left = [i for i in self.pending_reports.get(lesson, []) if i not in sent]
        if left:
            self.pending_reports[lesson] = left
        else:
            self.pending_reports.pop(lesson, None)

    def add_from_plan(self, plan: dict, today: date) -> int:
        """生成したレッスンの問いを足す. 既存の問いは置き換えも削除もしない.

        新出項目の問いは翌日から. 復習した項目の問いは、その項目がまだ一度も
        キューに入っていない (Discord で確かめる機会がなかった) ときだけ足す.
        足した数を返す."""
        new_ids = {i["id"] for i in plan.get("new_items", [])}
        queued = {i for e in self.entries.values() for i in e.items}
        due = (today + timedelta(days=1)).isoformat()
        added = 0
        for q in plan.get("review", []):
            items = list(q.get("items") or [])
            key = key_for(items)
            if not items or key in self.entries:
                continue
            is_new = bool(new_ids & set(items))
            if not is_new and set(items) <= queued:
                continue  # どの項目もすでに確認の予定がある
            self.entries[key] = Entry(
                items=items,
                prompt=q["prompt"],
                answer=q["answer"],
                source_lesson=plan["lesson_number"],
                new=is_new,
                due=due,
            )
            queued.update(items)
            added += 1
        return added

    # ---- 読み書き -------------------------------------------------------

    @classmethod
    def load(cls, path: Path, today: date) -> "ReviewQueue":
        """読む. 旧形式 ({"lesson", "questions"}) なら変換して書き直し、元のファイルは
        ``<path>.v1.bak`` に残す. ファイルがない・読めないときは空."""
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as e:
            # 次の保存で上書きしないよう、読めないファイルは脇へ退ける
            broken = path.with_name(path.name + ".broken")
            logging.error(f"振り返りキューを読めませんでした ({broken} に退避): {e}")
            path.replace(broken)
            return cls()
        if raw.get("format") == FORMAT:
            return cls(
                {k: Entry(**v) for k, v in raw.get("items", {}).items()},
                {int(n): ids for n, ids in raw.get("pending_reports", {}).items()},
            )
        queue = cls.from_legacy(raw, today)
        shutil.copyfile(path, path.with_name(path.name + ".v1.bak"))
        queue.save(path)
        return queue

    @classmethod
    def from_legacy(cls, raw: dict, today: date) -> "ReviewQueue":
        """旧形式の問いは未回答 (unseen)、期限は変換した日. 実際に言えたかは
        learner.json からは分からないため."""
        queue = cls()
        for q in raw.get("questions", []):
            items = list(q.get("items") or [])
            if items and key_for(items) not in queue.entries:
                queue.entries[key_for(items)] = Entry(
                    items=items,
                    prompt=q["prompt"],
                    answer=q["answer"],
                    source_lesson=int(raw.get("lesson", 0)),
                    due=today.isoformat(),
                )
        return queue

    def save(self, path: Path) -> None:
        data = {
            "format": FORMAT,
            "items": {k: asdict(e) for k, e in self.entries.items()},
            "pending_reports": {str(n): v for n, v in self.pending_reports.items()},
        }
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, path)
