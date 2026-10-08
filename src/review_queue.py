"""Discord 振り返りのキュー (issue #31).

音声レッスン側の間隔反復 (learner.json) とは別に、「いつ Discord で自己申告を
求めるか」を管理する。問いは一度入ったら消さず、答えるまで unseen のまま残る。
1 回の振り返りは上限まで、優先度の高いものから出す。

単位は項目ではなく問い: «Ég vil fara heim.» のように複数の項目を一度に言う問いは
1 件で、その結果は含まれる項目すべてに当てはめる (どちらで詰まったかは分けられない).
同じ項目が別の問いにも入ることがある.

答えた結果のうち音声レッスン側 (audiolesson report) にまだ届いていないものは
``pending_reports`` に出題元のレッスンごとに、結果 (言えた / 迷った / 言えなかった) 別に
残し、報告できたら消す. 音声レッスン側はどれも別々に扱う (issue #119).
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
Report = dict[str, list[str]]  # 結果 ("failed" / "shaky" / "ok") → 項目


def empty_report() -> Report:
    return {"failed": [], "shaky": [], "ok": []}


# 結果ごとの次の確認までの日数。同じ結果が続くたびに次の値へ進む (最後の値で頭打ち)
INTERVALS: dict[str, tuple[int, ...]] = {
    "failed": (1,),
    "shaky": (1, 3, 7),
    "ok": (1, 3, 7, 14, 30),
}
# 聞いただけの文 (language-learning-audio #183 の bonus) の問いを、1 回の振り返りに何件まで出すか
MAX_BONUS_PER_REVIEW = 2
# これだけ期限を過ぎた問いは「言えなかった」と同じ優先度に上げる (新出が毎回あっても埋もれない)
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
    # 音声レッスン側が未解決 (open) とした項目を含む問い (language-learning-audio #149).
    # 言えなかったと同じ優先度で出し、答えたら外す
    open: bool = False
    # 音声レッスン側で「試しに言ってもらっただけ」の文 (bonus). 言えたら加点、言えなくても何も起きない.
    # 一度だけ聞き、答えたら外す. 必ず答える問い (must_answer) にはしない
    bonus: bool = False

    def tier(self, today: date) -> int:
        """小さいほど先に出す. 期限前は 5.

        まだ答えていない新出の問いが最優先 (issue #119): 答えがなければ音声レッスン側は
        成功とみなすので、確かめずに済ませてしまわないよう、前回の新出を必ず先に聞く."""
        due = date.fromisoformat(self.due)
        if due > today:
            return 5
        if self.bonus:
            return 3  # 期限の来た 言えなかった / 未解決 / 新出のあと、普通の ok の復習より前
        if self.new and self.state == "unseen":
            return 0
        if self.state == "failed" or self.open:
            return 1
        tier = {"shaky": 2, "ok": 4}.get(self.state, 3)
        if tier > 1 and (today - due).days >= PROMOTE_AFTER_DAYS:
            tier = 1  # 長く待たされている
        return tier


def unaskable(entry: Entry) -> bool:
    """問いに埋まっていないテンプレートの «{» «}» が残っている (language-learning-audio #73: «{hour}»).
    次の plan か表示時の更新で言い直されるまで出さない."""
    return any(c in text for text in (entry.prompt, entry.answer) for c in "{}")


def key_for(items: list[str]) -> str:
    return "+".join(items)


@dataclass
class ReviewQueue:
    entries: dict[str, Entry] = field(default_factory=dict)
    # 出題元レッスン → まだ report していない結果別の項目. キーがあること自体が
    # 「このレッスンの問いに答えたが、まだ報告していない」を表す
    pending_reports: dict[int, Report] = field(default_factory=dict)

    # ---- 選ぶ ---------------------------------------------------------

    def latest_lesson(self) -> int:
        """キューに問いを足した最新のレッスン (直前に生成したレッスン)."""
        return max((e.source_lesson for e in self.entries.values()), default=0)

    def must_answer(self, today: date) -> list[str]:
        """必ず出す問い: 直前のレッスンの、まだ答えていない新出の問いと、期限の来た未解決
        (open) 項目の問い (language-learning-audio #220). 通常の /lesson ではこれに全部答えるまで
        次のレッスンを生成しない (答えがなければ音声レッスン側は成功とみなすので、新出は必ず
        確かめる. 未解決の項目は言えたと確かめるまで閉じないので、毎回確かめる).

        未解決項目の問いは項目ごとに 1 件 (add_from_plan が open を付けた問い). 同じ日にもう一度
        失敗した問いは期限が明日になるので、二度は出ない. 波括弧の残った問いは出せない (``unaskable``)."""
        latest = self.latest_lesson()
        return [
            k
            for k, e in self.entries.items()
            if not unaskable(e)
            and (
                (e.new and e.state == "unseen" and e.source_lesson == latest)
                or (e.open and e.tier(today) < 5)
            )
        ]

    def drop_stale_new(self) -> list[str]:
        """直前より前のレッスンの、答えないまま次のレッスンに進んだ新出の問いを外す
        (/lesson-auto を挟んだときなど). 音声レッスン側では成功とみなされ済みなので報告は
        要らない. 後の復習でその項目がまた出れば、通常の問いとしてキューに入り直す."""
        latest = self.latest_lesson()
        stale = [
            k
            for k, e in self.entries.items()
            if e.new and e.state == "unseen" and e.source_lesson < latest
        ]
        for k in stale:
            del self.entries[k]
        return stale

    def select(self, today: date, limit: int = 0) -> list[str]:
        """今回の振り返りで出す問いのキー. 必ず出す問い (must_answer: 直前のレッスンの未回答の新出と、期限の来た未解決項目) は
        limit を超えても全部、先頭に. limit > 0 なら残りを最大 limit 問まで、期限の来ている
        問いが足りなければ期限前の問いで埋める. limit = 0 なら期限の来ている問いすべて."""

        def order(k: str) -> tuple:
            e = self.entries[k]
            tier = e.tier(today)
            # 新出どうしは新しいレッスンから (前回の新出が上限で押し出されない)
            latest_first = -e.source_lesson if tier == 0 else 0
            return (
                tier,
                e.bonus,
                latest_first,
                e.due,
                e.last_reviewed or "",
            )  # 同じ tier では bonus が最後 (満席なら最初に外れる)

        required = sorted(self.must_answer(today), key=order)
        rest = [k for k in sorted(self.entries, key=order) if k not in required]
        for k in rest:
            if unaskable(self.entries[k]) and self.entries[k].tier(today) < 5:
                logging.warning(f"振り返りの問いに {{…}} が残っているので出しません: {k}")
        rest = [k for k in rest if not unaskable(self.entries[k])]
        bonus_seen = 0
        kept = []
        for k in rest:  # bonus は 1 回に MAX_BONUS_PER_REVIEW 件まで
            if self.entries[k].bonus and self.entries[k].tier(today) < 5:
                bonus_seen += 1
                if bonus_seen > MAX_BONUS_PER_REVIEW:
                    continue
            kept.append(k)
        rest = kept
        if limit <= 0:
            return required + [k for k in rest if self.entries[k].tier(today) < 5]
        return required + rest[: max(0, limit - len(required))]

    def due_count(self, today: date) -> int:
        return sum(
            1 for e in self.entries.values() if e.tier(today) < 5 and not unaskable(e)
        )

    # ---- 更新する -------------------------------------------------------

    def record(self, key: str, result: str, today: date) -> None:
        """1 問の結果. その問いの状態と次の期限だけを変える."""
        if result not in INTERVALS:
            raise ValueError(result)
        e = self.entries[key]
        e.streak = e.streak + 1 if e.state == result else 1
        e.open = False
        e.state = result
        e.reviews += 1
        e.last_reviewed = today.isoformat()
        e.due = (today + timedelta(days=next_interval(result, e.streak))).isoformat()
        ids = self.pending_reports.setdefault(e.source_lesson, empty_report())[result]
        ids += [i for i in e.items if i not in ids]

    def record_bonus(self, key: str, result: str) -> None:
        """bonus の問いの結果 (#183). 言えた (ok) だけを音声レッスン側へ報告する: その項目は
        「言えた」で、音声レッスン側が 1 回の成功として数える. 迷った / 言えなかったは何も報告せず
        (音声レッスン側には何も起きない). どちらでも問いは外す: 一度だけ聞く."""
        if result not in INTERVALS:
            raise ValueError(result)
        e = self.entries.pop(key)
        if result == "ok":
            ids = self.pending_reports.setdefault(e.source_lesson, empty_report())["ok"]
            ids += [i for i in e.items if i not in ids]

    def drop_stale_bonus(self, latest: int) -> list[str]:
        """答えないまま次の plan が来た bonus の問いを外す (積み上がらない)."""
        stale = [
            k for k, e in self.entries.items() if e.bonus and e.source_lesson < latest
        ]
        for k in stale:
            del self.entries[k]
        return stale

    # ---- 音声レッスン側への報告 -------------------------------------------

    def reports(self) -> list[tuple[int, Report]]:
        """まだ届いていない報告: (出題元レッスン, 結果別の項目) をレッスン順に."""
        return [
            (n, {k: list(v) for k, v in r.items()})
            for n, r in sorted(self.pending_reports.items())
        ]

    def mark_reported(self, lesson: int, sent: Report) -> None:
        """``lesson`` の報告が届いた. 送った後に増えた分 (報告中に答えた問い) は残す."""
        pending = self.pending_reports.get(lesson, empty_report())
        left = {
            k: [i for i in ids if i not in sent.get(k, [])]
            for k, ids in pending.items()
        }
        if any(left.values()):
            self.pending_reports[lesson] = left
        else:
            self.pending_reports.pop(lesson, None)

    def add_from_plan(self, plan: dict, today: date) -> int:
        """生成したレッスンの問いを足す. 既存の問いは削除しない.

        新出項目の問いはすぐ (同じ日の次の /lesson でも) 出す. 復習した項目の問いは、その項目がまだ一度も
        キューに入っていない (Discord で確かめる機会がなかった) ときだけ足す.
        既存の問いを plan がもう一度出してきたら、問い方 (prompt / answer) だけ新しい方に直す
        (期限・結果・source_lesson はそのまま: language-learning-audio #73).
        未解決 (open) の項目 (plan の open_items と open_not_fitted) は、すべて振り返りで確かめる
        (``_ask_open_items``). 足した数を返す."""
        new_ids = {i["id"] for i in plan.get("new_items", [])}
        open_ids = set(plan.get("open_items") or []) | set(
            plan.get("open_not_fitted") or []
        )
        self.drop_stale_bonus(plan["lesson_number"])
        queued = {i for e in self.entries.values() if not e.bonus for i in e.items}
        added = 0
        bonus_added = 0
        for q in plan.get("review", []):
            items = list(q.get("items") or [])
            if not items:
                continue
            if q.get("bonus"):
                # 聞いただけの文 (#183): 別の問いに同じ項目があっても独立した問いとして足し、明日出す
                key = f"bonus:{plan['lesson_number']}:{key_for(items)}"
                if key not in self.entries and bonus_added < MAX_BONUS_PER_REVIEW:
                    self.entries[key] = Entry(
                        items=items,
                        prompt=q["prompt"],
                        answer=q["answer"],
                        source_lesson=plan["lesson_number"],
                        due=(today + timedelta(days=1)).isoformat(),
                        bonus=True,
                    )
                    bonus_added += 1
                    added += 1
                continue
            key = key_for(items)
            is_open = bool(open_ids & set(items))
            if key in self.entries:
                entry = self.entries[key]
                entry.prompt = q.get("prompt") or entry.prompt
                entry.answer = q.get("answer") or entry.answer
                continue
            is_new = bool(new_ids & set(items))
            if not is_new and not is_open and set(items) <= queued:
                continue  # どの項目もすでに確認の予定がある
            due = (today if is_new else today + timedelta(days=1)).isoformat()
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
        if "open_items" in plan or "open_not_fitted" in plan:
            self._ask_open_items(open_ids, today)
        return added

    def refresh_wording(self, fresh: dict[str, dict]) -> int:
        """1 項目だけの問い (キーが項目 ID) のうち、古くなったものだけ今の言い方 (``fresh`` = {ID: {"prompt",
        "answer", "cues"}}, audiolesson questions の出力) に置き換える. 古いのは (1) 聞けない問い (波括弧が残る)、
        (2) 答えが今の答えと同じ (= 単語だけを聞く問い) で、問い方が今の言い方 (cues) のどれでもないもの.
        1 項目をキーにした文の問い (答えが文) は、文で復習する原則 (§9) と plan の言い方 (#73) のまま残す.
        期限・結果・source_lesson・open はそのまま. 直した数を返す."""
        changed = 0
        for key, now in fresh.items():
            e = self.entries.get(key)
            if e is None or e.bonus or e.items != [key] or not isinstance(now, dict):
                continue
            prompt, answer = now.get("prompt"), now.get("answer")
            if not (isinstance(prompt, str) and isinstance(answer, str) and prompt and answer):
                continue
            cues = now.get("cues")
            cues = [c for c in cues if isinstance(c, str)] if isinstance(cues, list) else []
            if not (unaskable(e) or (e.answer == answer and e.prompt not in (cues or [prompt]))):
                continue
            if (e.prompt, e.answer) != (prompt, answer):
                e.prompt, e.answer = prompt, answer
                changed += 1
        return changed

    def _ask_open_items(self, open_ids: set[str], today: date) -> None:
        """未解決の項目は、レッスンで練習したものも入りきらなかったものも、次の振り返りで一度確かめる
        (language-learning-audio #149, #220). 言えれば音声レッスン側で閉じ、レッスンの時間はかからない.
        言えなければ新しい失敗になる. 項目ごとに既存の問いを 1 件 (項目の少ないものを優先、同じなら新しい
        レッスンのもの) 選んで open にし、期限を明日に引き寄せる. 閉じた項目の問いは open を外す.
        問いがまだない項目は聞けない (plan が問いを持ってくるので、普通は足りている)."""
        chosen: set[str] = set()
        for item in sorted(open_ids):
            cands = [
                (len(e.items), -e.source_lesson, k)
                for k, e in self.entries.items()
                if item in e.items and not e.bonus
            ]
            if cands:
                chosen.add(min(cands)[2])
        for k, e in self.entries.items():
            if k in chosen:
                self._bring_forward(e, today)
            else:
                e.open = False  # 選ばれなかった問いと、閉じた項目の問いは open を外す (項目ごとに 1 件だけが open)

    @staticmethod
    def _bring_forward(entry: Entry, today: date) -> None:
        """未解決の項目 (音声レッスン側の open_items) を含む問いは、期限を明日に戻す.
        音声レッスン側は「言えた」と確かめられるまで項目を閉じない (language-learning-audio
        #149) ので、``ok`` の間隔 (7日、14日…) を待たせると何週間も開いたままになる.
        結果の履歴 (state・streak) はそのまま、次の確認の日だけ早め、優先度も上げる
        (``open``: 期限前でなくなれば ``failed`` と同じ tier. ``ok`` のままだと上限で最初に外される)."""
        tomorrow = (today + timedelta(days=1)).isoformat()
        if entry.due > tomorrow:
            entry.due = tomorrow
        entry.open = True

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
                {int(n): _report(r) for n, r in raw.get("pending_reports", {}).items()},
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


def _report(raw: list[str] | dict[str, list[str]]) -> Report:
    """保存された報告. 以前の形式 (言えなかった項目のリストだけ) も読む."""
    report = empty_report()
    if isinstance(raw, list):
        report["failed"] = list(raw)
    else:
        report.update({k: list(v) for k, v in raw.items() if k in report})
    return report
