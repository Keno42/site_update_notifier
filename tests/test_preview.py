"""Hearing a new expression before the lesson, and asking how it is said (the review of site_update_notifier#180): the new-expression list
has «🔊 発音を聞く・説明を頼む». The learner picks an expression, hears the lesson's own sentence as often as they like, and asks for pronunciation
help with #180's request (the expression and «発音» already chosen). Nothing is an outcome, a load or a schedule."""
import asyncio
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src import newlist
from src.feedback import Feedback, PreviewButton, Record, preview_text, preview_view
from tests.test_feedback import NOW, PLAN, saved

SENTENCES = dict(
    PLAN,
    exercises=[
        {"kind": "intro", "items": ["eg_var_ad_inf"], "label": "new pattern: Ég var að {inf}."},
        {"kind": "recall", "items": ["eg_var_ad_inf", "borda"], "label": "meaning: Ég var að borða."},
        {"kind": "recall", "items": ["takk_fyrir"], "label": "situation: Takk fyrir matinn."},
    ],
)


class Response:
    def __init__(self, log):
        self.log = log

    def is_done(self):
        return False

    async def send_message(self, content=None, **kw):
        self.log.append(("send", content, kw))

    async def edit_message(self, **kw):
        self.log.append(("edit", kw.get("content"), kw))

    async def defer(self, **kw):
        self.log.append(("defer", None, kw))

    async def send_modal(self, modal):
        self.log.append(("modal", None, {"modal": modal}))


class Followup:
    def __init__(self, log):
        self.log = log

    async def send(self, content=None, **kw):
        self.log.append(("followup", content, kw))


def interaction(log, user=1):
    return SimpleNamespace(user=SimpleNamespace(id=user), channel_id=5, response=Response(log), followup=Followup(log), channel=None)


class PreviewTests(unittest.TestCase):
    def test_the_sentence_played_is_the_lessons_own(self):
        record = Record(Path("."), {"lesson": 12}, SENTENCES)
        self.assertEqual(preview_text(record, "eg_var_ad_inf"), "Ég var að borða.", "a filled sentence, never the pattern")
        self.assertEqual(preview_text(record, "takk_fyrir"), "Takk fyrir matinn.")
        bare = Record(Path("."), {"lesson": 12}, dict(PLAN, new_items=[*PLAN["new_items"], {"id": "takk", "target": "Takk.", "meaning": "ありがとう。"}]))
        self.assertEqual(preview_text(bare, "takk"), "Takk.", "no sentence: the expression itself")
        self.assertIsNone(preview_text(bare, "eg_var_ad_inf"), "only a pattern: nothing is made up")

    def test_a_word_taught_inside_a_sentence_plays_the_sentence_without_the_labels_note(self):
        """Review of #183: lesson 40 played «Hvar er sjúkrahúsið? (sjúkrahúsið)» and «Ég er þrjátíu ára. (þrjátíu)»: the label's note."""
        plan = dict(PLAN, new_items=[{"id": "sjukrahusid", "target": "sjúkrahúsið", "meaning": "the hospital"}],
                    exercises=[{"index": 3, "kind": "intro", "items": ["sjukrahusid"], "label": "new: Hvar er sjúkrahúsið? (sjúkrahúsið)"}])
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "lesson-012.script.json").write_text(json.dumps({"segments": [
                {"type": "speak", "text": "Hvar er sjúkrahúsið?", "lang": "is", "exercise": 3},
                {"type": "speak", "text": "sjúkrahúsið", "lang": "is", "exercise": 3},
            ]}), "utf-8")
            self.assertEqual(preview_text(Record(Path(td), {"lesson": 12}, plan), "sjukrahusid"), "Hvar er sjúkrahúsið?")
        self.assertEqual(preview_text(Record(Path("."), {"lesson": 12}, plan), "sjukrahusid"), "Hvar er sjúkrahúsið?", "no script: the label without its note")

    def _open(self, td, speak_fails=False, explain_on=True, user=1):
        log, spoken = [], []
        ledger = saved(Path(td))

        @contextlib.asynccontextmanager
        async def speak(text):
            spoken.append(text)
            if speak_fails:
                raise RuntimeError("tts down")
            path = Path(td) / "x.mp3"
            path.write_bytes(b"mp3")
            yield path

        async def explain(messages):
            return "explained"

        fb = Feedback({1: "yuki"}, lambda n: Path(td) / n, 0, lambda: NOW, explain=explain if explain_on else None, speak=speak)
        PreviewButton.handler = fb
        record = ledger.load()
        record.plan["exercises"] = SENTENCES["exercises"]  # the lesson's own sentences
        fb._record = self._found(ledger, record)  # type: ignore[method-assign]
        asyncio.run(PreviewButton(1, record.id).callback(interaction(log, user)))
        return fb, ledger, log, spoken

    @staticmethod
    def _found(ledger, record):
        async def found(interaction, ref):
            return "yuki", ledger, record

        return found

    @staticmethod
    def labels(view):
        return [getattr(c, "label", None) or getattr(c, "placeholder", None) for c in view.children]

    def test_a_learner_picks_an_expression_and_hears_it_again_and_again(self):
        with tempfile.TemporaryDirectory() as td:
            fb, ledger, log, spoken = self._open(td)
            view = log[-1][2]["view"]
            self.assertEqual(self.labels(view), ["発音を確かめたい表現", "🔊 発音を聞く", "発音の説明を頼む"])
            self.assertIn("このチャンネルに投稿されます", log[-1][1])
            play = view.children[1]
            asyncio.run(play.callback(interaction(log)))
            self.assertIn("先に表現を選んでください", log[-1][1])
            view.select._values = ["eg_var_ad_inf"]
            asyncio.run(view.select.callback(interaction(log)))
            self.assertIn("読み上げる文：「Ég var að borða.」", log[-1][1])
            for _ in range(2):
                asyncio.run(play.callback(interaction(log)))
            self.assertEqual(spoken, ["Ég var að borða.", "Ég var að borða."])
            files = [entry for entry in log if entry[0] == "followup"]
            self.assertEqual([f[1] for f in files], ["🔊「Ég var að borða.」"] * 2)
            self.assertEqual(files[0][2]["file"].filename, "pronunciation.mp3")
            for f in files:
                f[2]["file"].close()
            self.assertEqual(ledger.events(), [], "hearing is no outcome")
            self.assertFalse((ledger.user_dir / "support_requests.jsonl").exists())

    def test_a_failed_playback_says_so_and_can_be_pressed_again(self):
        with tempfile.TemporaryDirectory() as td:
            _, _, log, spoken = self._open(td, speak_fails=True)
            view = log[-1][2]["view"]
            view.select._values = ["takk_fyrir"]
            asyncio.run(view.select.callback(interaction(log)))
            asyncio.run(view.children[1].callback(interaction(log)))
            self.assertEqual(log[-1][1], "音声を作れませんでした。もう一度「🔊 発音を聞く」を押してください。")
            self.assertEqual(self.labels(view)[1], "🔊 発音を聞く", "the button stays")

    def test_a_pronunciation_request_opens_with_the_expression_and_kind_chosen(self):
        with tempfile.TemporaryDirectory() as td:
            fb, ledger, log, _ = self._open(td)
            view = log[-1][2]["view"]
            view.select._values = ["eg_var_ad_inf"]
            asyncio.run(view.select.callback(interaction(log)))
            asyncio.run(view.children[2].callback(interaction(log)))
            modal = log[-1][2]["modal"]
            modal.question._value = "var の母音"

            async def submit():
                await modal.on_submit(interaction(log))
                await asyncio.gather(*fb.explanations)

            asyncio.run(submit())
            (line,) = [json.loads(x) for x in (ledger.user_dir / "support_requests.jsonl").read_text("utf-8").splitlines()]
            self.assertEqual(line["requests"], [{"kind": "pronunciation", "item_id": "eg_var_ad_inf", "question": "var の母音"}])
            self.assertIn("受け付けました（Ég var að {inf}.・発音）", log[-1][1])
            self.assertEqual(ledger.events(), [], "a request is no outcome")

    def test_only_the_learner_may_use_it_and_buttons_need_their_service(self):
        with tempfile.TemporaryDirectory() as td:
            _, _, log, _ = self._open(td, user=2)
            self.assertIn("このレッスンを受けた人だけ", log[-1][1])
        with tempfile.TemporaryDirectory() as td:
            _, _, log, _ = self._open(td, explain_on=False)
            self.assertEqual(self.labels(log[-1][2]["view"]), ["発音を確かめたい表現", "🔊 発音を聞く"], "no explainer: playback only")
            self.assertNotIn("このチャンネルに投稿されます", log[-1][1])

    def test_the_list_carries_the_button_and_an_old_list_does_not(self):
        sent = []

        class Channel:
            async def send(self, content=None, **kw):
                sent.append(kw.get("view"))

        newlist.list_view = preview_view
        try:
            entry = {"channel": 5, "message": 9, "text": "**レッスン 12 の新出表現**", "owner": 1, "manifest": "lesson-012"}
            asyncio.run(newlist.send(Channel(), entry))
            asyncio.run(newlist.send(Channel(), {k: v for k, v in entry.items() if k not in ("owner", "manifest")}))
        finally:
            newlist.list_view = None
        self.assertEqual(sent[0].children[0].item.custom_id, "lla-preview:1:lesson-012")
        self.assertIsNone(sent[1])
        with tempfile.TemporaryDirectory() as td:
            pending = newlist.PendingLists(Path(td))
            pending.add("lesson-012", 5, 9, "text", 12, owner=1)
            self.assertEqual({k: v for k, v in pending.take("lesson-012").items() if k in ("owner", "manifest")}, {"owner": 1, "manifest": "lesson-012"})


if __name__ == "__main__":
    unittest.main()
