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
import tempfile
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
# コマンドで指定するレッスン: 12 / 012 / lesson-012 / 12.2 / lesson-012.2
REF_RE = re.compile(r"(?:lesson-)?0*(\d+)(?:\.(\d+))?")

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
        return {
            i["id"]: i
            for i in self.plan.get("reviewed_items", [])
            + self.plan.get("new_items", [])
        }

    def new_items(self) -> list[dict]:
        return list(self.plan.get("new_items", []))

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
        "manifest": record.id,
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
    lines = [f"**{record.title} のフィードバック**（30秒ほど。負荷だけは必須）"]
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
        f"使えそう: {names(e.get('usable', []))}",
        f"早めにもう一度: {names(e.get('sooner', []))}",
    ]
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
            content=f"{self.record.title} のフィードバックを記録しました。ありがとうございます。",
            view=None,
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
    ) -> None:
        self.users = users
        self.user_dir = user_dir
        self.channel_id = channel_id
        self.now = now

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

    async def open_form(
        self, interaction: discord.Interaction, ref: str | int | None = None
    ) -> None:
        found = await self._record(interaction, ref)
        if found is None:
            return
        name, ledger, record = found

        async def submit(answers: Answers) -> None:
            ledger.append(build_event(record, answers, name, self.now()))

        await interaction.response.send_message(
            form_text(record),
            view=FeedbackView(record, interaction.user.id, submit),
            ephemeral=True,
        )

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

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner:
            await interaction.response.send_message(
                "このレッスンを受けた人だけがフィードバックを送れます。", ephemeral=True
            )
            return
        if FeedbackButton.handler is not None:
            await FeedbackButton.handler.open_form(interaction, self.manifest)


def feedback_view(owner: int, manifest: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(FeedbackButton(owner, manifest))
    return view
