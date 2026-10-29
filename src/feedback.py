"""レッスン後のフィードバック (language-learning-audio issue #128).

学習者の手応えを、レッスンの直後に 30 秒ほどで記録し、Discord だけで取り出せるようにする。
スケジュール (learner.json) とは分けて持ち、v1 では生成にも復習にも一切使わない。

置き場所は learner.json と同じユーザーディレクトリ (LESSON_ROOT の下、git の外) なので、
cron の自動更新 (git pull) で消えたり上書きされたりしない:

    <LESSON_ROOT>/<名前>/
      learner.json
      lesson_feedback.jsonl        追記のみ. 1 行 = 1 回のフィードバック
      lesson_manifests/
        lesson-012/                生成したときのまま変えない
          manifest.json            生成日時、bot と language-learning-audio のコミット、
                                   各ファイルの sha256、生成に使った引数
          lesson-012.plan.json
          lesson-012.script.json
          lesson-012.transcript.md
          learner.before.json      生成前の learner.json (選ばれ方を再現するため)
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

import discord

from . import newlist, usersettings
from .interaction import (
    CHOOSE_FAILED,
    NOTE_FAILED,
    SEND_FAILED,
    SEND_SCREEN_FAILED,
    answers_on_failure,
    screen_step,
)

SCHEMA = 1
FEEDBACK_FILE = "lesson_feedback.jsonl"
# #179: the help the learner asked for, one line per request; kept apart from the feedback ledger, so a request changes no outcome, no load and
# no schedule
SUPPORT_FILE = "support_requests.jsonl"
SUPPORT_KINDS = {"pronunciation": "発音", "grammar": "文法・使い方"}
MANIFESTS = "lesson_manifests"
LESSON_FILES = (".plan.json", ".script.json", ".transcript.md")
LEARNER_BEFORE = "learner.before.json"
MANIFEST_RE = re.compile(r"lesson-(\d+)(?:\.(\d+))?$")
# コマンドで指定するレッスン: 12 / 012 / lesson-012 / 12.2 / lesson-012.2
REF_RE = re.compile(r"(?:lesson-)?0*(\d+)(?:\.(\d+))?")

LOADS = {"light": "軽い", "right": "ちょうどいい", "heavy": "重い"}
# フォームの選択肢の文言. 保存するのは左のキーだけなので、言い回しは変えても記録は壊れない
LOAD_HINTS = {
    "light": "量が少なめ・易しめ",
    "right": "",
    "heavy": "量が多め・難しめ",
}
FRICTIONS = {
    "repetitive": "同じ表現がくり返し出すぎた",
    "unclear": "何を答えればいいか分からない問いがあった",
    "pacing": "テンポが合わない（答える時間・話す速さ）",
    "other": "その他（「メモを書く」で）",
}
# 「練習が足りなかった」表現が難しかった理由 (language-learning-audio, lesson 27: «Við erum fjögur» は聞き取りが、«Ég nenni ekki að sofa» は
# 使う場面が難しかった. 理由がメモにしかないと音声レッスンに届かない). レッスン単位で、選んだ表現すべてに当てはまるものを選ぶ
REASONS = {
    "say": "発音しにくい",
    "hear": "聞き取りにくい",
    "scene": "意味・使う場面が浮かばない",
    "remember": "覚えにくい",
}
MAX_CANDIDATES = 15  # 候補 + FRICTIONS が Discord の選択肢の上限 25 に収まるように
MESSAGE_MAX = 1900  # Discord の本文は 2000 字まで


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _manifest_key(d: Path) -> tuple[int, int] | None:
    m = MANIFEST_RE.match(d.name)
    return (int(m[1]), int(m[2] or 1)) if m else None


def parse_ref(ref: str | int | None) -> tuple[int, int | None] | None:
    """(レッスン番号, 何回目の記録か or None=最新). ref が None なら None、
    読めなければ ValueError.

    記録の ID (lesson-012 / lesson-012.2) はその記録だけを指す: lesson-012 は再生成が
    あっても 1 回目 (投稿のボタンはこれで呼ぶ). 番号だけ (12) なら最後の記録、12.2 は
    2 回目."""
    if ref is None:
        return None
    text = str(ref).strip()
    m = REF_RE.fullmatch(text)
    if m is None:
        raise ValueError(f"レッスンの指定が読めません: {ref!r}")
    if m[2]:
        return int(m[1]), int(m[2])
    return int(m[1]), 1 if text.startswith("lesson-") else None


class Ledger:
    """1 人分のフィードバックとレッスンの記録."""

    def __init__(self, user_dir: Path) -> None:
        self.user_dir = user_dir
        self.manifests = user_dir / MANIFESTS
        self.path = user_dir / FEEDBACK_FILE

    # ---- manifests

    def save_manifest(
        self,
        work: Path,
        plan: dict,
        learner_before: bytes | None,
        learner_after: Path,
        revisions: dict[str, str | None],
        args: list[str],
        now: datetime,
        trip_sha256: str | None = None,
    ) -> Path:
        """生成したレッスンの記録を残す. 同じ番号の記録があっても上書きせず、
        lesson-012.2 のように別に作る (生成済みの記録は変えない).

        trip_sha256: 生成に使った旅程のプロフィール (language-learning-audio #132) の
        ハッシュ. プロフィールの中身 (日程・行き先) は記録にもエクスポートにも入れない."""
        n = int(plan["lesson_number"])
        base = f"lesson-{n:03d}"
        d, k = self.manifests / base, 2
        while d.exists():
            d, k = self.manifests / f"{base}.{k}", k + 1
        d.mkdir(parents=True)
        files: dict[str, str] = {}
        for suffix in LESSON_FILES:
            src = work / f"{base}{suffix}"
            if src.exists():
                shutil.copy2(src, d / src.name)
                files[src.name] = sha256(d / src.name)
        if learner_before is not None:
            (d / LEARNER_BEFORE).write_bytes(learner_before)
            files[LEARNER_BEFORE] = sha256(d / LEARNER_BEFORE)
        manifest = {
            "schema": SCHEMA,
            "id": d.name,
            "lesson": n,
            "created_at": now.isoformat(timespec="seconds"),
            "revisions": revisions,
            "generate_args": args,
            "files": files,
            "learner_after_sha256": (
                sha256(learner_after) if learner_after.exists() else None
            ),
            "trip_sha256": trip_sha256,
        }
        (d / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8"
        )
        return d

    def save_extra(self, ref: str, name: str, data: dict) -> bool:
        """生成後にわかったことを、記録のディレクトリに別のファイルとして足す (確認待ちの内訳 ``review_backlog.json`` #123 など). 数だけ.
        manifest.json は生成したときのまま変えない. エクスポートは記録のディレクトリの全ファイルを持つので、そのまま載る. 失敗しても投稿は止めない (False)."""
        d = self.manifests / ref
        try:
            if not d.is_dir():
                return False
            (d / name).write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        except OSError:
            return False
        return True

    def _dirs(self) -> list[tuple[tuple[int, int], Path]]:
        if not self.manifests.exists():
            return []
        return sorted(
            (key, d)
            for d in self.manifests.iterdir()
            if d.is_dir() and (key := _manifest_key(d)) is not None
        )

    def manifest_dir(self, ref: str | int | None = None) -> Path | None:
        """記録のディレクトリ. ref が lesson-012.2 / 12.2 ならその記録、12 なら同じ番号の
        最後の記録、None なら最新のレッスン. フィードバックは記録 (再生成なら .2) に
        紐付けるので、投稿のボタンは記録の ID で呼ぶ."""
        dirs = self._dirs()
        want = parse_ref(ref)
        if want is not None:
            lesson, rev = want
            dirs = [
                (key, d)
                for key, d in dirs
                if key[0] == lesson and (rev is None or key[1] == rev)
            ]
        return dirs[-1][1] if dirs else None

    def siblings(self, record: "Record") -> list[str]:
        """同じ番号の記録 (再生成) の ID, 古い順."""
        return [d.name for key, d in self._dirs() if key[0] == record.lesson]

    def load(self, ref: str | int | None = None) -> "Record | None":
        d = self.manifest_dir(ref)
        if d is None:
            return None
        manifest = json.loads((d / "manifest.json").read_text("utf-8"))
        manifest.setdefault("id", d.name)
        plan_path = d / f"lesson-{manifest['lesson']:03d}.plan.json"
        plan = json.loads(plan_path.read_text("utf-8")) if plan_path.exists() else {}
        return Record(d, manifest, plan)

    # ---- feedback events

    def append(self, event: dict) -> None:
        """1 行追記して fsync する. 既存の行は書き換えない. 前回の書き込みが電源断などで
        途中で切れていたら (末尾が改行でない)、まず改行を足して区切る: 切れた行だけが
        読めなくなり、今回の行は巻き込まれない."""
        self.user_dir.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        with open(self.path, "ab") as f:
            if f.tell() > 0:
                with open(self.path, "rb") as r:
                    r.seek(-1, os.SEEK_END)
                    if r.read(1) != b"\n":
                        line = b"\n" + line
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def events(self, manifest: str | None = None) -> list[dict]:
        """読めた行だけ. 途中で切れた行 (UTF-8 の文字の途中で切れたものも) は 1 行ずつ
        読み飛ばすので、ほかの行は読める. manifest を渡すとその記録へのものだけ."""
        if not self.path.exists():
            return []
        out = []
        for raw in self.path.read_bytes().split(b"\n"):
            try:
                event = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            if manifest is None or event.get("manifest") == manifest:
                out.append(event)
        return out

    def export(self, ref: str | int | None, out_dir: Path) -> Path | None:
        """その記録へのフィードバックと記録一式を zip にする (GitHub や外部の分析用)."""
        record = self.load(ref)
        if record is None:
            return None
        out = out_dir / f"{record.id}-feedback.zip"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(
                "feedback.jsonl",
                "".join(
                    json.dumps(e, ensure_ascii=False) + "\n"
                    for e in self.events(record.id)
                ),
            )
            for p in sorted(record.dir.iterdir()):
                z.write(p, f"{record.dir.name}/{p.name}")
        return out


@dataclass
class Record:
    """保存済みのレッスン 1 回分."""

    dir: Path
    manifest: dict
    plan: dict

    @property
    def lesson(self) -> int:
        return int(self.manifest["lesson"])

    @property
    def id(self) -> str:
        """lesson-012 / 再生成なら lesson-012.2."""
        return str(self.manifest.get("id") or self.dir.name)

    @property
    def title(self) -> str:
        rev = _manifest_key(self.dir)
        return f"レッスン {self.lesson}" + (
            f"（再生成 {rev[1]} 回目）" if rev and rev[1] > 1 else ""
        )

    def items(self) -> dict[str, dict]:
        """id → {id, target, meaning, kind}: every item the plan describes. ``exercise_items`` (language-learning-audio, every item an exercise names) is
        absent from an older plan, which describes only the new and the reviewed items."""
        return {
            i["id"]: i
            for i in self.plan.get("exercise_items", [])
            + self.plan.get("reviewed_items", [])
            + self.plan.get("new_items", [])
        }

    def new_items(self) -> list[dict]:
        """今日の新出 (同じ id は 1 つ, #78)."""
        return newlist.unique_items(list(self.plan.get("new_items", [])))

    def reviewed_choices(self, limit: int) -> list[dict]:
        """新出以外で、このレッスンで言わせた (recall / connect / generative の問いに入った) 項目. 未解決 (open) の項目を先に、
        あとはレッスンで最初に問われた順に、``limit`` 件まで (language-learning-audio, lesson 28: 復習で言えなかった表現を選べなかった).
        対象は問いから決める: reviewed_items (復習に選ばれた項目) だけでは、つなぎの問いの «Hæ.» や文の中の «japönsku» が抜ける (#151 review).
        説明 (target) のない id は出さない (exercise_items のない古い plan). 新出は含めない (それは new_items の欄)."""
        if limit <= 0:
            return []
        new = {i["id"] for i in self.new_items()}
        described = self.items()
        asked: list[str] = []
        for e in self.plan.get("exercises", []):
            if e.get("kind") in ("recall", "connect", "generative"):
                for i in e.get("items", []):
                    if i not in new and i in described and i not in asked:
                        asked.append(i)
        open_ids = set(self.plan.get("open_items") or [])
        order = [i for i in asked if i in open_ids] + [i for i in asked if i not in open_ids]
        return [described[i] for i in order[:limit]]

    def candidates(self) -> list[dict]:
        """カードに出す候補 (MAX_CANDIDATES 件まで). 同じ場面の繰り返し (古いレッスンの
        repeated_situation) は練習なので出さない."""
        cands = self.plan.get("review_candidates", [])
        return [c for c in cands if c.get("kind") != "repeated_situation"][
            :MAX_CANDIDATES
        ]

    def describe(self, c: dict) -> str:
        items = self.items()
        target = "、".join(
            (items.get(i, {}).get("target") or i) for i in c.get("items", [])
        )
        kind = c.get("kind")
        if kind == "early_last_appearance":
            last, end = c.get("last_s") or 0, c.get("end_s") or 0
            return (
                f"後半に出てこなかった気がする: {target}"
                f"（最後に出たのは約{last / 60:.0f}分ごろ。全{end / 60:.0f}分中）"
            )
        if kind == "no_late_recall":
            return f"終わり近くに、ヒントなしで言う場面がなかった: {target}"
        return f"{kind}: {target}"

    def digests(self) -> dict[str, str | None]:
        files = self.manifest.get("files", {})
        stem = f"lesson-{self.lesson:03d}"
        return {
            "transcript_sha256": files.get(f"{stem}.transcript.md"),
            "script_sha256": files.get(f"{stem}.script.json"),
            "learner_sha256": files.get(LEARNER_BEFORE),
        }


# ---------------------------------------------------------------- the form


def _clip(text: str, n: int = 100) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


@dataclass
class Answers:
    unheard: list[str] = field(
        default_factory=list
    )  # 以前のフォームの項目. 欄は外した (#81) が、記録の形は変えない: 常に []
    sooner: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)  # REASONS のキー: 「練習が足りなかった」表現が難しかった理由
    load: str | None = None
    concerns: list[str] = field(default_factory=list)  # "c<番号>" / "f:<種類>"
    note: str = ""


# 新出のこれ以上の割合を「覚えていない」と選んだら、負荷は「重い」として数える
HEAVY_SHARE = 0.6


def effective_load(record: "Record", answers: "Answers") -> str | None:
    """負荷の評価. 新出の大半を「練習が足りなかった・覚えていない」と選んだなら、選んだ負荷が何であっても
    「重い」として数える: 何も覚えていないのは、その人にとって量の合図であって、項目ごとの事実ではない
    (#81). ペースへの入力にするのは language-learning-audio #218 で、ここでは記録するだけ."""
    new = record.new_items()
    new_ids = {i["id"] for i in new}
    if new and len(set(answers.sooner) & new_ids) / len(new) >= HEAVY_SHARE:  # 新出のうちの割合: 復習の項目は数えない
        return "heavy"
    return answers.load


def build_event(record: Record, answers: Answers, user: str, now: datetime) -> dict:
    shown = record.candidates()
    confirmed = [
        shown[int(v[1:])]
        for v in answers.concerns
        if v.startswith("c") and v[1:].isdigit() and int(v[1:]) < len(shown)
    ]
    return {
        "schema": SCHEMA,
        "ts": now.isoformat(timespec="seconds"),
        "user": user,
        "lesson": record.lesson,
        "manifest": record.id,
        "revisions": record.manifest.get("revisions", {}),
        **record.digests(),
        "unheard": answers.unheard,
        "sooner": answers.sooner,
        "sooner_reasons": [r for r in answers.reasons if r in REASONS],
        "load": answers.load,
        "load_effective": effective_load(record, answers),
        "friction": [v[2:] for v in answers.concerns if v.startswith("f:")],
        "candidates_shown": shown,
        "candidates_confirmed": confirmed,
        "note": answers.note,
    }


EXPLAIN_SYSTEM = (
    "あなたは、アイスランド旅行の準備をしている日本語話者の大人に、アイスランド語を教える先生です。"
    "音声レッスンのあとで学習者が「練習が足りなかった」と選んだ表現を、日本語で短く解説してください。"
    "表現ごとに、意味と、旅行者がその表現を言いそうな具体的な場面を一つ。"
    "理由に「発音しにくい」「聞き取りにくい」があれば、その表現の音のつながりや、聞き取りのコツを一言。"
    "「意味・使う場面が浮かばない」があれば場面を丁寧に。実際には言いそうにない組み合わせなら、そうはっきり書いてください。"
    "全体で 1200 字以内、見出しは表現そのもの。"
)


def explain_messages(record: "Record", answers: "Answers", skip: set[str] | frozenset[str] = frozenset()) -> list[dict]:
    """「練習が足りなかった」表現の解説を LLM に頼む messages. 表現と意味と選んだ理由だけを送る (メモや学習者の記録は送らない).
    ``skip``: 解説を頼んだ表現 (#179: 同じ表現の解説が二つチャンネルに出ないように). 何も残らなければ []."""
    items = record.items()
    chosen = [items.get(i) or {"id": i} for i in dict.fromkeys(answers.sooner) if i not in skip]
    if not chosen:
        return []
    lines = [f"・{c.get('target') or c['id']}（{c.get('meaning') or ''}）" for c in chosen]
    reasons = [REASONS[r] for r in answers.reasons if r in REASONS]
    user = "選んだ表現:\n" + "\n".join(lines) + "\n難しかった理由: " + ("、".join(reasons) if reasons else "（選んでいない）")
    return [{"role": "system", "content": EXPLAIN_SYSTEM}, {"role": "user", "content": user}]


SUPPORT_SYSTEM = (
    "あなたは、アイスランド旅行の準備をしている日本語話者の大人に、アイスランド語を教える先生です。"
    "学習者が選んだ表現一つについて、頼まれた種類の解説だけを日本語で短く書いてください。"
    "学習者の質問があれば、まずそれに答え、その部分に絞ってください（例:「einhleyp の後半」なら、その音だけを説明する）。"
    "「発音」なら、難しい部分の綴りがどう読まれるか、強勢の位置、日本語話者が間違えやすい点を具体的に。"
    "カタカナの読みは添えないでください（母音が足されて別の音になります）。確かでないことは書かないでください。"
    "「文法・使い方」なら、文の組み立て（どの語がどの形で、なぜその形か）と、別の言い方か使う場面を一つ。"
    "レッスンで使った文があれば、その文で説明してください。"
    "「もっと詳しく」と頼まれたら前の解説を踏まえて詳しく、「別の説明で」と頼まれたら前の解説とは別の角度で説明してください。"
    "全体で 1200 字以内、見出しは表現そのもの。"
)
SUPPORT_OTHER = "_other"  # 選択肢にない表現: 質問欄に書いてもらう
SUPPORT_FOLLOW = {"more": "もっと詳しく", "other": "別の説明で"}


def lesson_sentences(record: "Record", item_id: str, limit: int = 2) -> list[str]:
    """レッスンで ``item_id`` を言った文 (問いのラベルの文, 型の {…} を残したものは除く): 型 «Ég var að {inf}.» ではなく実際の文で解説するため."""
    out: list[str] = []
    for e in record.plan.get("exercises", []):
        if item_id not in e.get("items", []):
            continue
        text = str(e.get("label", "")).split(": ", 1)[-1].strip()
        if text and "{" not in text and ": " in str(e.get("label", "")) and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def support_label(record: "Record", request: dict) -> str:
    """「表現・種類」: 受け付けと回答の見出し."""
    if request["item_id"] == SUPPORT_OTHER:
        what = "選択肢にない表現"
    else:
        it = record.items().get(request["item_id"]) or {"id": request["item_id"]}
        what = it.get("target") or it["id"]
    return f"{what}・{SUPPORT_KINDS.get(request['kind'], request['kind'])}"


def support_messages(record: "Record", request: dict, previous: str = "") -> list[dict]:
    """頼まれた解説 (#179) を LLM に頼む messages: 表現一つと意味、頼まれた種類、レッスンで使った文、学習者の質問だけを送る.
    「もっと詳しく」「別の説明で」 (request["follow"]) なら前の解説も送る."""
    if request["item_id"] == SUPPORT_OTHER:
        head = "（選択肢にない表現。学習者の質問を見てください）"
    else:
        it = record.items().get(request["item_id"]) or {"id": request["item_id"]}
        head = f"{it.get('target') or it['id']}（{it.get('meaning') or ''}）"
    lines = [f"表現: {head}", f"種類: {SUPPORT_KINDS.get(request['kind'], request['kind'])}"]
    if request["item_id"] != SUPPORT_OTHER and (sents := lesson_sentences(record, request["item_id"])):
        lines.append("レッスンで使った文: " + " / ".join(sents))
    if request.get("question"):
        lines.append("学習者の質問: " + request["question"])
    if request.get("follow") in SUPPORT_FOLLOW and previous:
        lines.append("頼み: " + SUPPORT_FOLLOW[request["follow"]])
        lines.append("前の解説:\n" + previous)
    return [{"role": "system", "content": SUPPORT_SYSTEM}, {"role": "user", "content": "\n".join(lines)}]


SUPPORT_INTRO = (
    "解説してほしい表現を1つと、解説の種類を選んで「頼む」を押してください。知りたいことを書く欄が開きます（空欄でも構いません）。\n"
    "回答はこのチャンネルに投稿されます（チャンネルのほかの人にも見えます）。練習の予定や負荷には数えません。"
)


class SupportModal(discord.ui.Modal, title="解説を頼む"):
    question: discord.ui.TextInput = discord.ui.TextInput(
        label="特にどの部分？何が知りたい？（任意）",
        placeholder="例：einhleyp の後半",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
    )

    def __init__(self, view: "SupportView") -> None:
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        question = str(self.question.value or "").strip()
        if self.view.item == SUPPORT_OTHER and not question:
            await interaction.response.send_message("選択肢にない表現は、質問欄に表現と知りたいことを書いてください。", ephemeral=True)
            return
        self.view.stop()
        await self.view.request(interaction, {"kind": self.view.kind, "item_id": self.view.item, "question": question})


class SupportView(discord.ui.View):
    """解説を頼む (#179): 表現を1つ (新出・復習、または選択肢にない表現) と種類 (発音・文法) を選び、「頼む」で質問欄を開く.
    表現ごとに種類を選べるよう、1回の頼みは1つの表現 (review of #180: 複数×複数は要らない解説を生んだ). 練習の予定にも負荷にも数えない."""

    def __init__(self, record: "Record", owner_id: int, request: Callable[[discord.Interaction, dict], Awaitable[None]]) -> None:
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.request = request
        self.item = ""
        self.kind = ""
        choices = record.new_items()[:24]
        choices += record.reviewed_choices(24 - len(choices))
        options = [
            discord.SelectOption(label=_clip(i.get("target") or i["id"]), value=i["id"], description=_clip(i.get("meaning") or "") or None)
            for i in choices
        ]
        options.append(discord.SelectOption(label="その他（質問欄に書く）", value=SUPPORT_OTHER, description="聞いただけの表現など"))
        self.item_select: discord.ui.Select = discord.ui.Select(placeholder="解説してほしい表現（1つ）", options=options, min_values=1, max_values=1)
        self.kind_select: discord.ui.Select = discord.ui.Select(
            placeholder="解説の種類", options=[discord.SelectOption(label=v, value=k) for k, v in SUPPORT_KINDS.items()], min_values=1, max_values=1
        )

        @answers_on_failure(CHOOSE_FAILED)
        async def chose_item(interaction: discord.Interaction) -> None:
            self.item = self.item_select.values[0]
            await interaction.response.defer()

        @answers_on_failure(CHOOSE_FAILED)
        async def chose_kind(interaction: discord.Interaction) -> None:
            self.kind = self.kind_select.values[0]
            await interaction.response.defer()

        self.item_select.callback = chose_item  # type: ignore[method-assign]
        self.kind_select.callback = chose_kind  # type: ignore[method-assign]
        self.add_item(self.item_select)
        self.add_item(self.kind_select)
        ask: discord.ui.Button = discord.ui.Button(label="頼む", style=discord.ButtonStyle.primary)
        ask.callback = self._ask  # type: ignore[method-assign]
        self.add_item(ask)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("解説はレッスンを受けた人だけが頼めます。", ephemeral=True)
            return False
        return True

    @answers_on_failure(SEND_FAILED)
    async def _ask(self, interaction: discord.Interaction) -> None:
        if not self.item or not self.kind:
            await interaction.response.send_message("表現と解説の種類を選んでから「頼む」を押してください。", ephemeral=True)
            return
        await interaction.response.send_modal(SupportModal(self))


class OwnerButtons(discord.ui.View):
    """レッスンを受けた人だけが押せるボタンの列: (label, callback) を並べる. 解説の回答・失敗の知らせ・受け付けの返事に付ける."""

    def __init__(self, owner_id: int, buttons: list[tuple[str, Callable[[discord.Interaction], Awaitable[None]]]]) -> None:
        super().__init__(timeout=3600)
        self.owner_id = owner_id
        for label, callback in buttons:
            button: discord.ui.Button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)
            button.callback = answers_on_failure(SEND_FAILED)(callback)  # type: ignore[method-assign]
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("解説はレッスンを受けた人だけが頼めます。", ephemeral=True)
            return False
        return True


def form_text(
    record: Record, answers: Answers | None = None, compact: bool = False
) -> str:
    """compact: 短いレッスンのフォーム (当てはまること・気になった点の欄がないので、その候補も書かない)."""
    lines = [
        f"**{record.title} のフィードバック**（30秒ほど。必須なのは「今日の量・難しさ」だけです）",
        "下の欄で当てはまるものを選んで、「送信」を押してください（選ばなくていい欄は飛ばして構いません）。",
    ]
    new = record.new_items()
    if new:
        lines.append("今日の新しい表現:")
        lines += [
            f"・{i.get('target') or i['id']}（{i.get('meaning') or ''}）" for i in new
        ]
    cands = [] if compact else record.candidates()
    if cands:
        lines.append(
            "レッスンの記録から機械が気づいたこと（当てはまるかどうかだけ、いちばん下の欄で教えてください）:"
        )
        lines += [f"・{record.describe(c)}" for c in cands]
    if answers and answers.note:
        lines.append(f"メモ: {_clip(answers.note, 200)}")
    return _clip("\n".join(lines), MESSAGE_MAX)


def report_text(record: Record, events: list[dict], siblings: list[str]) -> str:
    others = [s for s in siblings if s != record.id]
    note = f"\n（同じ番号の別の記録: {'、'.join(others)}）" if others else ""
    if not events:
        return f"{record.title} のフィードバックはまだありません。{note}"
    e = events[-1]
    items = record.items()

    def names(ids: list[str]) -> str:
        return "、".join(items.get(i, {}).get("target") or i for i in ids) or "なし"

    rev = e.get("revisions") or {}
    lines = [
        f"**{record.title} のフィードバック**（{len(events)}件、最新 {e.get('ts', '?')}）",
        f"負荷: {LOADS.get(e.get('load') or '', e.get('load') or '未回答')}",
    ]
    if e.get("unheard"):  # 以前のフォームの項目 (#81 で外した)
        lines.append(f"出てこなかった・聞こえなかった: {names(e['unheard'])}")
    if e.get("usable"):  # 以前のフォームの項目 (いま言えそうな表現)
        lines.append(f"使えそう: {names(e['usable'])}")
    lines.append(f"練習が足りなかった・覚えていない: {names(e.get('sooner', []))}")
    if e.get("load_effective") and e["load_effective"] != e.get("load"):
        counted = LOADS.get(e["load_effective"], e["load_effective"])
        lines.append(f"（選んだ表現が多いので負荷は「{counted}」として数えます）")
    if e.get("candidates_confirmed"):
        lines.append(
            "当てはまった候補: "
            + " / ".join(record.describe(c) for c in e["candidates_confirmed"])
        )
    shown = len(e.get("candidates_shown", []))
    if shown:
        lines.append(
            f"（候補 {shown} 件中 {len(e.get('candidates_confirmed', []))} 件）"
        )
    if e.get("friction"):
        lines.append(
            "気になった点: " + "、".join(FRICTIONS.get(f, f) for f in e["friction"])
        )
    if e.get("note"):
        lines.append(f"メモ: {e['note']}")
    lines.append(
        f"生成: bot `{(rev.get('bot') or '?')[:7]}` / "
        f"language-learning-audio `{(rev.get('lla') or '?')[:7]}`（{record.id}）" + note
    )
    return _clip("\n".join(lines), MESSAGE_MAX)


class NoteModal(discord.ui.Modal, title="メモ（任意）"):
    note: discord.ui.TextInput = discord.ui.TextInput(
        label="気づいたこと",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
    )

    def __init__(self, form: "FeedbackView") -> None:
        super().__init__()
        self.form = form
        self.note.default = form.answers.note

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.form.answers.note = str(self.note.value or "").strip()
        await interaction.response.edit_message(
            content=form_text(self.form.record, self.form.answers), view=self.form
        )


def lesson_minutes(record: "Record") -> float:
    """レッスンの長さ (plan.json の config.minutes). なければ BASE_MINUTES."""
    minutes = (record.plan.get("config") or {}).get("minutes")
    return float(minutes or usersettings.BASE_MINUTES)


class FeedbackView(discord.ui.View):
    """練習が足りなかった新出・今日の量と難しさ・当てはまることと気になった点・メモ・送信.
    compact (短いレッスン) なら、当てはまること・気になった点とメモは出さない."""

    def __init__(
        self,
        record: Record,
        owner_id: int,
        submit: Callable[[Answers], Awaitable[None]],
        compact: bool = False,
        request_support: Callable[[discord.Interaction, dict], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(timeout=900)
        self.record = record
        self.owner_id = owner_id
        self.submit = submit
        self.request_support = request_support
        self.answers = Answers()
        new = record.new_items()[:25]
        reviewed = record.reviewed_choices(25 - len(new))
        if new or reviewed:
            # 「ほとんど見かけなかった」の欄は外した (一度も使われず、問いの意図が伝わらなかった, #81). 残るのは否定側
            # だけ: 聞いた直後の「覚えた」はあてにならない (次の日の振り返りが確かめる) が、「思い出せない」は当てになる.
            # 選ばなかった項目は「不満なし」で、「十分」ではない
            self._select(
                "sooner",
                "練習が足りなかった・言えなかった表現（新出・復習、複数可・なければ選ばなくてOK）",
                [
                    discord.SelectOption(
                        label=_clip(i.get("target") or i["id"]),
                        value=i["id"],
                        description=_clip(i.get("meaning") or "") or None,
                    )
                    for i in new
                ]
                + [
                    # 復習で言えなかった表現も選べる (lesson 28). 印は説明の頭に付ける: ラベルは表現そのまま
                    discord.SelectOption(
                        label=_clip(i.get("target") or i["id"]),
                        value=i["id"],
                        description=_clip("復習: " + (i.get("meaning") or "")),
                    )
                    for i in reviewed
                ],
                min_values=0,
            )
            self._select(
                "reasons",
                "選んだ表現が難しかった理由（複数可・選ばなくてOK）",
                [discord.SelectOption(label=v, value=k) for k, v in REASONS.items()],
                min_values=0,
            )
        self._select(
            "load",
            "今日のレッスンの量・難しさ（必須）",
            [
                discord.SelectOption(
                    label=v, value=k, description=LOAD_HINTS.get(k) or None
                )
                for k, v in LOADS.items()
            ],
            min_values=1,
            max_values=1,
        )
        send: discord.ui.Button = discord.ui.Button(
            label="送信", style=discord.ButtonStyle.primary, row=4
        )
        send.callback = self._send  # type: ignore[method-assign]
        if request_support is not None and (new or reviewed):
            # #179: help on an expression, asked for apart from «練習が足りなかった»: no extra practice, no load
            helpme: discord.ui.Button = discord.ui.Button(label="解説を頼む", row=4)
            helpme.callback = self._help  # type: ignore[method-assign]
            self.add_item(helpme)
        if compact:
            self.add_item(send)
            return
        concerns = [
            discord.SelectOption(label=_clip(record.describe(c)), value=f"c{n}")
            for n, c in enumerate(record.candidates())
        ] + [
            discord.SelectOption(label=v, value=f"f:{k}") for k, v in FRICTIONS.items()
        ]
        self._select(
            "concerns",
            "当てはまること・気になった点（複数可・選ばなくてもOK）",
            concerns,
            min_values=0,
        )
        note: discord.ui.Button = discord.ui.Button(label="メモを書く", row=4)
        note.callback = self._note  # type: ignore[method-assign]
        self.add_item(note)
        self.add_item(send)

    def _select(
        self,
        attr: str,
        placeholder: str,
        options: list[discord.SelectOption],
        min_values: int,
        max_values: int | None = None,
    ) -> None:
        select: discord.ui.Select = discord.ui.Select(
            placeholder=placeholder,
            options=options,
            min_values=min_values,
            max_values=max_values or len(options),
        )

        @answers_on_failure(CHOOSE_FAILED)
        async def callback(interaction: discord.Interaction) -> None:
            value: Any = list(select.values)
            if attr == "load":
                value = value[0] if value else None
            setattr(self.answers, attr, value)
            await interaction.response.defer()

        select.callback = callback  # type: ignore[method-assign]
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "このフィードバックはレッスンを受けた人だけが送れます。", ephemeral=True
            )
            return False
        return True

    @answers_on_failure(NOTE_FAILED)
    async def _note(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(NoteModal(self))

    @answers_on_failure(CHOOSE_FAILED)
    async def _help(self, interaction: discord.Interaction) -> None:
        assert self.request_support is not None
        await interaction.response.send_message(SUPPORT_INTRO, view=SupportView(self.record, self.owner_id, self.request_support), ephemeral=True)

    @answers_on_failure(SEND_FAILED)
    async def _send(self, interaction: discord.Interaction) -> None:
        if self.answers.load is None:
            await interaction.response.send_message(
                "「今日の量・難しさ」を選んでから送信してください。", ephemeral=True
            )
            return
        await self.submit(self.answers)  # 記録 (失敗なら SEND_FAILED: まだ押し直せる)
        self.stop()
        # ここから先は画面だけ: 失敗しても記録は済んでいる
        await screen_step(
            lambda: interaction.response.edit_message(
                content=f"{self.record.title} のフィードバックを記録しました。ありがとうございます。",
                view=None,
            ),
            lambda: interaction.followup.send(SEND_SCREEN_FAILED, ephemeral=True),
        )


# ---------------------------------------------------------------- entry points


class Feedback:
    """Discord からの入口: 投稿のボタン、/lesson-feedback、report、export.

    /lesson と同じく登録ユーザーだけ、LESSON_CHANNEL_ID があればそのチャンネルでだけ.
    export は learner.json やメモを含むので本人にだけ見える形で返す."""

    def __init__(
        self,
        users: dict[int, str],
        user_dir: Callable[[str], Path],
        channel_id: int = 0,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        report_feedback: Callable[[str, int, list[str], str | None], Awaitable[bool]] | None = None,
        explain: Callable[[list[dict]], Awaitable[str]] | None = None,
        wants_explain: Callable[[str], bool] = lambda name: False,
    ) -> None:
        self.users = users
        self.user_dir = user_dir
        self.channel_id = channel_id
        self.now = now
        # 送信で選んだことを 1 回の report で音声レッスンへ報告する: 「練習が足りなかった・覚えていない」の項目は
        # 早めにもう一度出す依頼 (`--sooner`: 間隔の半分以内に。結果としては数えない, language-learning-audio #222)、
        # 負荷はペースの入力 (`--load`, #218). (名前, レッスン番号, 項目, 実効の負荷) → 受け付けたか
        self.report_feedback = report_feedback
        # /lesson-configure の「フィードバック後の解説」: 選んだ表現を、選んだ理由に合わせて LLM が解説する (チャットと同じ LLM: messages → 返答)
        self.explain = explain
        self.wants_explain = wants_explain
        self.explanations: set[asyncio.Task] = set()  # the explanations still waiting for the LLM (held so they are not collected while they run)
        self.submitted: set[tuple[str, str]] = set()  # (user, manifest) whose feedback was sent: a help request's answer says whether it still waits

    def ledger(self, name: str) -> Ledger:
        return Ledger(self.user_dir(name))

    async def _deny(self, interaction: discord.Interaction, text: str) -> None:
        await interaction.response.send_message(text, ephemeral=True)

    async def _record(
        self, interaction: discord.Interaction, ref: str | int | None
    ) -> tuple[str, Ledger, Record] | None:
        """使える人・チャンネルか確かめ、指定の記録を読む. だめなら返事をして None."""
        name = self.users.get(interaction.user.id)
        if name is None:
            await self._deny(
                interaction, "このコマンドは登録されたユーザーだけが使えます。"
            )
            return None
        if self.channel_id and interaction.channel_id != self.channel_id:
            await self._deny(interaction, f"<#{self.channel_id}> で実行してください。")
            return None
        try:
            parse_ref(ref)
        except ValueError:
            await self._deny(
                interaction,
                "レッスンは 12 や 12.2（再生成した記録）のように指定してください。",
            )
            return None
        ledger = self.ledger(name)
        record = ledger.load(ref)
        if record is None:
            which = f"レッスン {ref} の" if ref is not None else "レッスンの"
            await self._deny(
                interaction,
                f"{which}記録がありません（この機能より前に生成したレッスンは対象外です）。",
            )
            return None
        return name, ledger, record

    async def _report_feedback(
        self,
        interaction: discord.Interaction,
        name: str,
        record: Record,
        answers: Answers,
    ) -> None:
        """記録できた後で、選んだ項目 (早めにもう一度) と実効の負荷 (選んだ項目が新出の大半なら「重い」, 60% 以上) を
        1 回の report で報告する. 何も選ばなくても負荷は送る. 失敗してもフィードバックの記録は成功のまま:
        失敗は黙らずチャンネルに知らせる (選んだことは記録に残っているので、あとから送り直せる)."""
        if self.report_feedback is None:
            return
        ids = list(dict.fromkeys(answers.sooner))
        load = effective_load(record, answers)
        if not ids and not load:
            return
        try:
            ok = await self.report_feedback(name, record.lesson, ids, load)
        except Exception:
            logging.exception("フィードバックの報告に失敗しました")
            ok = False
        if not ok and interaction.channel is not None:
            try:
                await interaction.channel.send(
                    "フィードバックは記録しましたが、学習状態へはまだ反映できていません"
                    "（もう一度 /lesson-feedback から送ると反映されます）。"
                )
            except Exception:
                logging.exception("報告失敗の通知も送れませんでした")

    async def _explain(self, interaction: discord.Interaction, name: str, record: Record, answers: Answers) -> None:
        """設定で解説を選んだ人に、「練習が足りなかった」表現の解説をチャンネルに出す. 記録の後なので、失敗しても記録は成功のまま."""
        messages = explain_messages(record, answers, skip=self._requested(name, record))
        channel = getattr(interaction, "channel", None)
        if self.explain is None or not messages or channel is None or not self.wants_explain(name):
            return
        try:
            text = (await self.explain(messages)).strip()
            if text:
                await channel.send(_clip(f"**{record.title} で選んだ表現の解説**\n{text}", MESSAGE_MAX))
        except Exception:
            logging.exception("フィードバックの解説を出せませんでした")

    def _requested(self, name: str, record: Record) -> set[str]:
        """このレッスンで解説を頼んだ表現 (support_requests.jsonl)."""
        path = self.ledger(name).user_dir / SUPPORT_FILE
        if not path.exists():
            return set()
        out: set[str] = set()
        for raw in path.read_text("utf-8").splitlines():
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if line.get("manifest") == record.id:
                out.update(r.get("item_id", "") for r in line.get("requests", []))
        return out

    async def _support(self, interaction: discord.Interaction, name: str, record: Record, request: dict, previous: str = "") -> None:
        """頼まれた解説 (#179): 記録してすぐに受け付けたと答え (どこに届くか、フィードバックはまだか)、解説は LLM が返したらチャンネルに出す.
        回答には「もっと詳しく」「別の説明で」「別の表現を質問する」、失敗 (空の返事も) には「再試行」. 練習の予定にも負荷にも数えない
        (フィードバックの記録とは別のファイル). 自動では頼み直さない."""
        line = {
            "schema": SCHEMA, "ts": self.now().isoformat(timespec="seconds"), "user": name,
            "lesson": record.lesson, "manifest": record.id, "requests": [request],
        }  # fmt: skip
        path = self.ledger(name).user_dir / SUPPORT_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        owner = interaction.user.id
        label = support_label(record, request)

        async def another(inter: discord.Interaction) -> None:
            await inter.response.send_message(
                SUPPORT_INTRO, view=SupportView(record, owner, lambda i, r: self._support(i, name, record, r)), ephemeral=True
            )

        ack = f"解説リクエストを受け付けました（{label}）。回答はこのチャンネルに投稿します。"
        if (name, record.id) not in self.submitted:
            ack += "\nレッスンのフィードバックはまだ送信されていません。"
        await interaction.response.send_message(ack, view=OwnerButtons(owner, [("別の表現も質問する", another)]), ephemeral=True)
        channel = getattr(interaction, "channel", None)

        async def explain() -> None:
            try:
                text = (await self.explain(support_messages(record, request, previous))).strip()  # type: ignore[misc]
                if not text:
                    raise ValueError("an empty explanation")
            except Exception:
                logging.exception("頼まれた解説を出せませんでした")
                if channel is not None:

                    async def retry(inter: discord.Interaction) -> None:
                        await self._support(inter, name, record, request, previous)

                    try:
                        await channel.send(
                            f"頼まれた解説を作れませんでした（{label}）。頼んだ内容は保存してあります。「再試行」で頼み直せます。",
                            view=OwnerButtons(owner, [("再試行", retry)]),
                        )
                    except Exception:
                        logging.exception("解説の失敗も知らせられませんでした")
                return
            if channel is None:
                return

            def follow(kind: str) -> Callable[[discord.Interaction], Awaitable[None]]:
                async def run(inter: discord.Interaction) -> None:
                    await self._support(inter, name, record, {**request, "follow": kind}, text)

                return run

            try:
                await channel.send(
                    _clip(f"**{record.title} で頼んだ解説：{label}**\n{text}", MESSAGE_MAX),
                    view=OwnerButtons(owner, [("もっと詳しく", follow("more")), ("別の説明で", follow("other")), ("別の表現を質問する", another)]),
                )
            except Exception:
                logging.exception("頼まれた解説を送れませんでした")

        task = asyncio.create_task(explain())
        self.explanations.add(task)
        task.add_done_callback(self.explanations.discard)

    @answers_on_failure()
    async def open_form(
        self, interaction: discord.Interaction, ref: str | int | None = None
    ) -> None:
        found = await self._record(interaction, ref)
        if found is None:
            return
        name, ledger, record = found

        async def submit(answers: Answers) -> None:
            ledger.append(build_event(record, answers, name, self.now()))
            self.submitted.add((name, record.id))
            await self._report_feedback(interaction, name, record, answers)
            # 記録できた後に、新出表現の一覧を出す (#79). 出せなくても記録は成功のまま
            pending = newlist.PendingLists(ledger.user_dir).take(record.dir.name)
            if pending is not None and interaction.channel is not None:
                await newlist.release(interaction.channel, [pending])
            # the explanation waits for the LLM: started here and not awaited, so the button is answered and the form closed within Discord's 3 seconds
            # (review of #150); it is posted to the channel when it comes
            task = asyncio.create_task(self._explain(interaction, name, record, answers))
            self.explanations.add(task)
            task.add_done_callback(self.explanations.discard)

        async def request_support(inter: discord.Interaction, request: dict) -> None:
            await self._support(inter, name, record, request)

        compact = lesson_minutes(record) < usersettings.COMPACT_FEEDBACK_BELOW
        await interaction.response.send_message(
            form_text(record, compact=compact),
            view=FeedbackView(record, interaction.user.id, submit, compact=compact, request_support=request_support if self.explain else None),
            ephemeral=True,
        )

    @answers_on_failure()
    async def report(
        self, interaction: discord.Interaction, ref: str | int | None = None
    ) -> None:
        found = await self._record(interaction, ref)
        if found is None:
            return
        _, ledger, record = found
        await interaction.response.send_message(
            report_text(record, ledger.events(record.id), ledger.siblings(record))
        )

    @answers_on_failure()
    async def export(
        self, interaction: discord.Interaction, ref: str | int | None = None
    ) -> None:
        found = await self._record(interaction, ref)
        if found is None:
            return
        _, ledger, record = found
        with tempfile.TemporaryDirectory() as td:
            path = ledger.export(record.id, Path(td))
            assert path is not None
            data = io.BytesIO(path.read_bytes())
        await interaction.response.send_message(
            f"{record.title}（{record.id}）のフィードバックと記録一式です。",
            file=discord.File(data, filename=path.name),
            ephemeral=True,
        )


class FeedbackButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"lla-feedback:(?P<owner>[0-9]+):(?P<manifest>lesson-[0-9]+(?:\.[0-9]+)?)",
):
    """レッスン投稿のボタン. custom_id にレッスンを受けた人の Discord ID と記録の ID
    (lesson-012、再生成なら lesson-012.2) を持つ: bot を再起動 (自動更新) した後でも
    押せ、同じ番号を再生成した後でも、押した投稿のレッスンに紐付く
    (client.add_dynamic_items で登録). 押せるのはその人だけ."""

    handler: "Feedback | None" = None

    def __init__(self, owner: int, manifest: str) -> None:
        super().__init__(
            discord.ui.Button(
                label="フィードバック",
                style=discord.ButtonStyle.secondary,
                custom_id=f"lla-feedback:{owner}:{manifest}",
            )
        )
        self.owner = owner
        self.manifest = manifest

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> "FeedbackButton":
        return cls(int(match["owner"]), match["manifest"])

    @answers_on_failure()
    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner:
            await interaction.response.send_message(
                "このレッスンを受けた人だけがフィードバックを送れます。", ephemeral=True
            )
            return
        if FeedbackButton.handler is not None:
            await FeedbackButton.handler.open_form(interaction, self.manifest)


def feedback_view(
    owner: int, manifest: str, list_button: bool = False
) -> discord.ui.View:
    """レッスン投稿のボタン. list_button: 一覧をフィードバックまで取っておくとき「新出表現をすぐ表示」も."""
    view = discord.ui.View(timeout=None)
    view.add_item(FeedbackButton(owner, manifest))
    if list_button:
        view.add_item(newlist.NewListButton(owner, manifest))
    return view
