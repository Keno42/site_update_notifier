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

import hashlib
import io
import json
import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

import discord

SCHEMA = 1
FEEDBACK_FILE = "lesson_feedback.jsonl"
MANIFESTS = "lesson_manifests"
LESSON_FILES = (".plan.json", ".script.json", ".transcript.md")
LEARNER_BEFORE = "learner.before.json"
MANIFEST_RE = re.compile(r"lesson-(\d+)(?:\.(\d+))?$")

LOADS = {"light": "軽い", "right": "ちょうどいい", "heavy": "重い"}
FRICTIONS = {
    "repetitive": "繰り返しが多い",
    "unclear": "何を答えればいいか分かりにくい",
    "pacing": "テンポ（速い・遅い・待ち時間）",
    "other": "その他（メモに）",
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
    ) -> Path:
        """生成したレッスンの記録を残す. 同じ番号の記録があっても上書きせず、
        lesson-012.2 のように別に作る (生成済みの記録は変えない)."""
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
        }
        (d / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8"
        )
        return d

    def manifest_dir(self, lesson: int | None = None) -> Path | None:
        """lesson の記録 (同じ番号が複数あれば最後のもの). None なら最新のレッスン."""
        if not self.manifests.exists():
            return None
        keyed = [
            (key, d)
            for d in self.manifests.iterdir()
            if d.is_dir() and (key := _manifest_key(d)) is not None
        ]
        if lesson is not None:
            keyed = [(key, d) for key, d in keyed if key[0] == lesson]
        return max(keyed)[1] if keyed else None

    def load(self, lesson: int | None = None) -> "Record | None":
        d = self.manifest_dir(lesson)
        if d is None:
            return None
        manifest = json.loads((d / "manifest.json").read_text("utf-8"))
        plan_path = d / f"lesson-{manifest['lesson']:03d}.plan.json"
        plan = json.loads(plan_path.read_text("utf-8")) if plan_path.exists() else {}
        return Record(d, manifest, plan)

    # ---- feedback events

    def append(self, event: dict) -> None:
        """1 行追記して fsync する. 既存の行は読み書きしない."""
        self.user_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def events(self, lesson: int | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text("utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # 書きかけの行 (電源断など) は読み飛ばす
            if lesson is None or event.get("lesson") == lesson:
                out.append(event)
        return out

    def export(self, lesson: int, out_dir: Path) -> Path | None:
        """そのレッスンのフィードバックと記録一式を zip にする (GitHub や外部の分析用)."""
        record = self.load(lesson)
        events = self.events(lesson)
        if record is None and not events:
            return None
        out = out_dir / f"lesson-{lesson:03d}-feedback.zip"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(
                "feedback.jsonl",
                "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
            )
            if record is not None:
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

    def items(self) -> dict[str, dict]:
        return {
            i["id"]: i
            for i in self.plan.get("reviewed_items", [])
            + self.plan.get("new_items", [])
        }

    def new_items(self) -> list[dict]:
        return list(self.plan.get("new_items", []))

    def candidates(self) -> list[dict]:
        """カードに出す候補. 同じ場面の繰り返しは回数の多い順に、全体で MAX_CANDIDATES 件まで."""
        cands = list(self.plan.get("review_candidates", []))
        repeats = sorted(
            (c for c in cands if c.get("kind") == "repeated_situation"),
            key=lambda c: -int(c.get("count", 0)),
        )
        others = [c for c in cands if c.get("kind") != "repeated_situation"]
        return (others + repeats)[:MAX_CANDIDATES]

    def describe(self, c: dict) -> str:
        items = self.items()
        target = "、".join(
            (items.get(i, {}).get("target") or i) for i in c.get("items", [])
        )
        kind = c.get("kind")
        if kind == "repeated_situation":
            return f"同じ場面が{c.get('count')}回: {target}"
        if kind == "early_last_appearance":
            last, end = c.get("last_s") or 0, c.get("end_s") or 0
            return f"最後に出たのが早い: {target}（{last / 60:.0f}分ごろ／全{end / 60:.0f}分）"
        if kind == "no_late_recall":
            return f"終盤にヒントなしで言う機会がない: {target}"
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
    usable: list[str] = field(default_factory=list)
    sooner: list[str] = field(default_factory=list)
    load: str | None = None
    concerns: list[str] = field(default_factory=list)  # "c<番号>" / "f:<種類>"
    note: str = ""


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
        "manifest": record.manifest.get("id"),
        "revisions": record.manifest.get("revisions", {}),
        **record.digests(),
        "usable": answers.usable,
        "sooner": answers.sooner,
        "load": answers.load,
        "friction": [v[2:] for v in answers.concerns if v.startswith("f:")],
        "candidates_shown": shown,
        "candidates_confirmed": confirmed,
        "note": answers.note,
    }


def form_text(record: Record, answers: Answers | None = None) -> str:
    lines = [
        f"**レッスン {record.lesson} のフィードバック**（30秒ほど。負荷だけは必須）"
    ]
    new = record.new_items()
    if new:
        lines.append(
            "新出: "
            + "、".join(
                f"{i.get('target') or i['id']}（{i.get('meaning') or ''}）" for i in new
            )
        )
    cands = record.candidates()
    if cands:
        lines.append("レッスンから見つかった候補（当てはまったら下で選んでください）:")
        lines += [f"・{record.describe(c)}" for c in cands]
    if answers and answers.note:
        lines.append(f"メモ: {_clip(answers.note, 200)}")
    return _clip("\n".join(lines), MESSAGE_MAX)


def report_text(record: Record | None, events: list[dict], lesson: int) -> str:
    if not events:
        return f"レッスン {lesson} のフィードバックはまだありません。"
    e = events[-1]
    items = record.items() if record else {}

    def names(ids: list[str]) -> str:
        return "、".join(items.get(i, {}).get("target") or i for i in ids) or "なし"

    rev = e.get("revisions") or {}
    lines = [
        f"**レッスン {lesson} のフィードバック**（{len(events)}件、最新 {e.get('ts', '?')}）",
        f"負荷: {LOADS.get(e.get('load') or '', e.get('load') or '未回答')}",
        f"使えそう: {names(e.get('usable', []))}",
        f"早めにもう一度: {names(e.get('sooner', []))}",
    ]
    if e.get("candidates_confirmed"):
        describe = record.describe if record else (lambda c: str(c.get("kind")))
        lines.append(
            "当てはまった候補: "
            + " / ".join(describe(c) for c in e["candidates_confirmed"])
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
        f"language-learning-audio `{(rev.get('lla') or '?')[:7]}`"
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


class FeedbackView(discord.ui.View):
    """新出の複数選択 2 つ・負荷・当てはまった候補と気になった点・メモ・送信."""

    def __init__(
        self,
        record: Record,
        owner_id: int,
        submit: Callable[[Answers], Awaitable[None]],
    ) -> None:
        super().__init__(timeout=900)
        self.record = record
        self.owner_id = owner_id
        self.submit = submit
        self.answers = Answers()
        new = record.new_items()[:25]
        if new:
            for attr, placeholder in (
                ("usable", "今使えそうな新出（複数可・なしでも可）"),
                ("sooner", "早めにもう一度聞きたい新出（複数可・なしでも可）"),
            ):
                self._select(
                    attr,
                    placeholder,
                    [
                        discord.SelectOption(
                            label=_clip(i.get("target") or i["id"]),
                            value=i["id"],
                            description=_clip(i.get("meaning") or "") or None,
                        )
                        for i in new
                    ],
                    min_values=0,
                )
        self._select(
            "load",
            "全体の負荷（必須）",
            [discord.SelectOption(label=v, value=k) for k, v in LOADS.items()],
            min_values=1,
            max_values=1,
        )
        concerns = [
            discord.SelectOption(label=_clip(record.describe(c)), value=f"c{n}")
            for n, c in enumerate(record.candidates())
        ] + [
            discord.SelectOption(label=v, value=f"f:{k}") for k, v in FRICTIONS.items()
        ]
        self._select(
            "concerns", "当てはまったこと・気になった点（任意）", concerns, min_values=0
        )
        note: discord.ui.Button = discord.ui.Button(label="メモを書く", row=4)
        note.callback = self._note  # type: ignore[method-assign]
        send: discord.ui.Button = discord.ui.Button(
            label="送信", style=discord.ButtonStyle.primary, row=4
        )
        send.callback = self._send  # type: ignore[method-assign]
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

    async def _note(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(NoteModal(self))

    async def _send(self, interaction: discord.Interaction) -> None:
        if self.answers.load is None:
            await interaction.response.send_message(
                "全体の負荷を選んでから送信してください。", ephemeral=True
            )
            return
        await self.submit(self.answers)
        self.stop()
        await interaction.response.edit_message(
            content=f"レッスン {self.record.lesson} のフィードバックを記録しました。ありがとうございます。",
            view=None,
        )


# ---------------------------------------------------------------- entry points


class Feedback:
    """Discord からの入口: 投稿のボタン、/lesson-feedback、report、export."""

    def __init__(
        self,
        users: dict[int, str],
        user_dir: Callable[[str], Path],
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self.users = users
        self.user_dir = user_dir
        self.now = now

    def ledger(self, name: str) -> Ledger:
        return Ledger(self.user_dir(name))

    async def _name(self, interaction: discord.Interaction) -> str | None:
        name = self.users.get(interaction.user.id)
        if name is None:
            await interaction.response.send_message(
                "このコマンドは登録されたユーザーだけが使えます。", ephemeral=True
            )
        return name

    async def open_form(
        self, interaction: discord.Interaction, lesson: int | None = None
    ) -> None:
        name = await self._name(interaction)
        if name is None:
            return
        ledger = self.ledger(name)
        record = ledger.load(lesson)
        if record is None:
            which = f"レッスン {lesson}" if lesson is not None else "レッスン"
            await interaction.response.send_message(
                f"{which}の記録がありません（この機能より前に生成したレッスンは対象外です）。",
                ephemeral=True,
            )
            return

        async def submit(answers: Answers) -> None:
            ledger.append(build_event(record, answers, name, self.now()))

        await interaction.response.send_message(
            form_text(record),
            view=FeedbackView(record, interaction.user.id, submit),
            ephemeral=True,
        )

    def _lesson(self, ledger: Ledger, lesson: int | None) -> int | None:
        if lesson is not None:
            return lesson
        record = ledger.load()
        return record.lesson if record else None

    async def report(
        self, interaction: discord.Interaction, lesson: int | None = None
    ) -> None:
        name = await self._name(interaction)
        if name is None:
            return
        ledger = self.ledger(name)
        n = self._lesson(ledger, lesson)
        if n is None:
            await interaction.response.send_message(
                "記録されたレッスンがありません。", ephemeral=True
            )
            return
        await interaction.response.send_message(
            report_text(ledger.load(n), ledger.events(n), n)
        )

    async def export(
        self,
        interaction: discord.Interaction,
        lesson: int | None = None,
        tmp: Path | None = None,
    ) -> None:
        name = await self._name(interaction)
        if name is None:
            return
        ledger = self.ledger(name)
        n = self._lesson(ledger, lesson)
        out_dir = tmp or (ledger.user_dir / "work")
        out_dir.mkdir(parents=True, exist_ok=True)
        path = ledger.export(n, out_dir) if n is not None else None
        if path is None:
            which = f"レッスン {n} の" if n is not None else "レッスンの"
            await interaction.response.send_message(
                f"{which}記録がありません。", ephemeral=True
            )
            return
        data = io.BytesIO(path.read_bytes())
        path.unlink(missing_ok=True)
        await interaction.response.send_message(
            f"レッスン {n} のフィードバックと記録一式です。",
            file=discord.File(data, filename=path.name),
        )


class FeedbackButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"lla-feedback:(?P<owner>[0-9]+):(?P<lesson>[0-9]+)",
):
    """レッスン投稿のボタン. custom_id にレッスンを受けた人の Discord ID とレッスン番号を
    持つので、bot を再起動 (自動更新) した後の投稿でも押せる (client.add_dynamic_items
    で登録). 押せるのはその人だけ."""

    handler: "Feedback | None" = None

    def __init__(self, owner: int, lesson: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="フィードバック",
                style=discord.ButtonStyle.secondary,
                custom_id=f"lla-feedback:{owner}:{lesson}",
            )
        )
        self.owner = owner
        self.lesson = lesson

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> "FeedbackButton":
        return cls(int(match["owner"]), int(match["lesson"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner:
            await interaction.response.send_message(
                "このレッスンを受けた人だけがフィードバックを送れます。", ephemeral=True
            )
            return
        if FeedbackButton.handler is not None:
            await FeedbackButton.handler.open_form(interaction, self.lesson)


def feedback_view(owner: int, lesson: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(FeedbackButton(owner, lesson))
    return view
