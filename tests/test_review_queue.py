"""src/review_queue.py のテスト (issue #31).

実行: python -m unittest discover -s tests -t .
"""

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.review_queue import (
    FORMAT,
    PROMOTE_AFTER_DAYS,
    Entry,
    ReviewQueue,
    next_interval,
)

D = date(2026, 9, 26)


def entry(state="unseen", due=D, new=False, last=None, items=None):
    return Entry(
        items=items or ["x"],
        prompt="p",
        answer="a",
        source_lesson=1,
        state=state,
        new=new,
        last_reviewed=last,
        due=due.isoformat(),
    )


def plan(n, new, review):
    return {
        "lesson_number": n,
        "new_items": [{"id": i} for i in new],
        "review": [
            {"items": items, "prompt": f"say {'+'.join(items)}", "answer": "A."}
            for items in review
        ],
    }


class PolicyTests(unittest.TestCase):
    def test_intervals(self):
        self.assertEqual(
            [next_interval("ok", n) for n in range(1, 7)], [1, 3, 7, 14, 30, 30]
        )
        self.assertEqual([next_interval("shaky", n) for n in range(1, 5)], [1, 3, 7, 7])
        self.assertEqual(next_interval("failed", 3), 1)

    def test_record_updates_only_that_entry(self):
        q = ReviewQueue({"a": entry(), "b": entry()})
        q.record("a", "ok", D)
        self.assertEqual(q.entries["a"].due, (D + timedelta(days=3)).isoformat(), "#133: the first ok starts at the second step: 3 days")
        q.record("a", "ok", D + timedelta(days=1))
        a, b = q.entries["a"], q.entries["b"]
        self.assertEqual((a.state, a.streak, a.reviews), ("ok", 3, 2))
        self.assertEqual(
            a.due, (D + timedelta(days=8)).isoformat(), "second ok: 7 days"
        )
        self.assertEqual(a.last_reviewed, (D + timedelta(days=1)).isoformat())
        self.assertEqual((b.state, b.reviews, b.due), ("unseen", 0, D.isoformat()))
        q.record("a", "shaky", D + timedelta(days=4))
        self.assertEqual(
            (a.state, a.streak), ("shaky", 1), "a different result restarts"
        )
        self.assertEqual(a.due, (D + timedelta(days=5)).isoformat())

    def test_shaky_comes_back_sooner_than_ok(self):
        q = ReviewQueue({"s": entry(), "o": entry()})
        for _ in range(4):
            q.record("s", "shaky", D)
            q.record("o", "ok", D)
            self.assertLessEqual(q.entries["s"].due, q.entries["o"].due)
        self.assertLess(q.entries["s"].due, q.entries["o"].due, "7 days vs 14 days")


class SelectionTests(unittest.TestCase):
    def test_priority_order(self):
        q = ReviewQueue(
            {
                "later": entry(due=D + timedelta(days=2)),
                "ok": entry("ok"),
                "old_unseen": entry(),
                "shaky": entry("shaky"),
                "new": entry(new=True),
                "failed": entry("failed"),
            }
        )
        self.assertEqual(
            q.select(D, limit=10),
            ["new", "failed", "shaky", "old_unseen", "ok", "later"],
        )
        self.assertEqual(
            q.select(D, limit=0),
            ["new", "failed", "shaky", "old_unseen", "ok"],
            "without a limit only what is due",
        )

    def test_the_last_lessons_new_items_come_first(self):
        """Issue #119: an unanswered item counts as recalled on the audio side, so the last
        lesson's new items are asked first — before failures, before long-waiting
        questions, and before an older lesson's still unanswered new items."""
        waiting = {f"old{i}": entry("ok", due=D - timedelta(days=30)) for i in range(5)}
        q = ReviewQueue(waiting | {"f": entry("failed")})
        older = entry(new=True, due=D - timedelta(days=5))
        older.source_lesson = 6
        q.entries["older_new"] = older
        q.add_from_plan(plan(7, ["a", "b"], [["a"], ["b"]]), D)
        self.assertEqual(
            q.select(D, limit=3), ["a", "b", "older_new"], "the same day too"
        )
        q.record("a", "ok", D)
        self.assertNotIn("a", q.select(D, limit=3), "once answered, no longer first")

    def test_every_new_item_of_the_last_lesson_is_asked_past_the_limit(self):
        """#38 review: /lesson generates only after the last lesson's new items are all
        answered, so a session holds all of them even beyond the limit."""
        q = ReviewQueue({"f": entry("failed")})
        q.add_from_plan(plan(7, list("abcde"), [[i] for i in "abcde"]), D)
        self.assertEqual(sorted(q.must_answer(D)), list("abcde"))
        self.assertEqual(
            sorted(q.select(D, limit=3)), list("abcde"), "all five, no room"
        )
        self.assertEqual(q.select(D, limit=6)[-1], "f", "the rest fill what is left")
        for k in "abcde":
            q.record(k, "ok", D)
        self.assertEqual(q.must_answer(D), [])

    def test_an_older_lessons_unanswered_new_items_leave_the_queue(self):
        """#38 review: a new item left unanswered when the next lesson was generated (a
        /lesson-auto in between) was presumed recalled by the audio lesson; it leaves the
        queue instead of waiting forever behind newer new items. Answered and non-new
        questions stay, and the item can come back as an ordinary question later."""
        q = ReviewQueue()
        q.add_from_plan(plan(6, ["old", "done"], [["old"], ["done"], ["rev"]]), D)
        q.record("done", "shaky", D)
        q.add_from_plan(plan(7, ["new"], [["new"]]), D + timedelta(days=1))
        self.assertEqual(q.drop_stale_new(), ["old"])
        self.assertEqual(sorted(q.entries), ["done", "new", "rev"])
        self.assertEqual(q.drop_stale_new(), [], "the last lesson's are kept")
        q.add_from_plan(plan(8, [], [["old"]]), D + timedelta(days=2))
        self.assertFalse(q.entries["old"].new, "back as an ordinary question")

    def test_ties_earliest_due_then_oldest_review(self):
        q = ReviewQueue(
            {
                "b": entry("ok", due=D, last="2026-09-20"),
                "c": entry("ok", due=D - timedelta(days=1), last="2026-09-25"),
                "a": entry("ok", due=D, last="2026-09-10"),
            }
        )
        self.assertEqual(q.select(D), ["c", "a", "b"])

    def test_limit_and_carry_over(self):
        q = ReviewQueue({f"q{i}": entry(items=[f"i{i}"]) for i in range(5)})
        first = q.select(D, limit=2)
        self.assertEqual(len(first), 2)
        for k in first:
            q.record(k, "ok", D)
        left = [k for k, e in q.entries.items() if e.state == "unseen"]
        self.assertEqual(
            len(left), 3, "not asked: still unreviewed, not passed or removed"
        )
        second = q.select(D + timedelta(days=1), limit=2)
        self.assertTrue(
            set(second) <= set(left), "tomorrow asks the carried-over ones first"
        )

    def test_long_waiting_items_are_not_starved_by_new_ones(self):
        """The last lesson's new items fill a session first (#119); with room left over, an
        overdue question still reaches the front within PROMOTE_AFTER_DAYS. (A session whose
        limit the new items alone use up asks only them.)"""
        q = ReviewQueue({"old": entry("ok", due=D)})
        asked_on = None
        for day in range(PROMOTE_AFTER_DAYS + 2):
            today = D + timedelta(days=day)
            q.add_from_plan(
                plan(
                    day + 2,
                    [f"n{day}_{i}" for i in range(4)],
                    [[f"n{day}_{i}"] for i in range(4)],
                ),
                today - timedelta(days=1),
            )
            for k in q.select(today, limit=5):
                if k == "old":
                    asked_on = day
                q.record(k, "ok", today)
            if asked_on is not None:
                break
        self.assertIsNotNone(
            asked_on, "an overdue item reaches the front despite daily new items"
        )
        self.assertLessEqual(asked_on, PROMOTE_AFTER_DAYS)


class ReportBookkeepingTests(unittest.TestCase):
    """PR #32 review: answers wait in the queue, per source lesson, until the audio
    lesson side (audiolesson report) has them."""

    def queue(self):
        def at(lesson, item):
            e = entry(items=[item])
            e.source_lesson = lesson
            return e

        return ReviewQueue(
            {"a": at(3, "a"), "b": at(7, "b"), "c": at(7, "c"), "d": at(7, "d")}
        )

    def test_answers_are_grouped_by_source_lesson_and_outcome(self):
        """Issue #119: every outcome is reported, not only failures — the audio lesson
        schedules recalled, hesitated and not recalled differently."""
        q = self.queue()
        q.record("a", "failed", D)
        q.record("b", "ok", D)
        q.record("c", "shaky", D)
        self.assertEqual(
            q.reports(),
            [
                (3, {"failed": ["a"], "shaky": [], "ok": []}),
                (7, {"failed": [], "shaky": ["c"], "ok": ["b"]}),
            ],
        )
        q.mark_reported(3, q.reports()[0][1])
        self.assertEqual([n for n, _ in q.reports()], [7])

    def test_a_reported_lesson_leaves_the_queue(self):
        q = self.queue()
        q.record("b", "ok", D)
        sent = q.reports()[0][1]
        self.assertEqual(sent, {"failed": [], "shaky": [], "ok": ["b"]})
        q.mark_reported(7, sent)
        self.assertEqual(q.reports(), [])

    def test_answers_given_while_a_report_is_in_flight_are_kept(self):
        q = self.queue()
        q.record("c", "failed", D)
        sent = q.reports()[0][1]
        q.record("d", "shaky", D)  # answered during the CLI call
        q.mark_reported(7, sent)
        self.assertEqual(q.reports(), [(7, {"failed": [], "shaky": ["d"], "ok": []})])

    def test_pending_reports_survive_a_restart(self):
        q = self.queue()
        q.record("a", "failed", D)
        q.record("b", "ok", D)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pending_review.json"
            q.save(path)
            self.assertEqual(ReviewQueue.load(path, D).reports(), q.reports())

    def test_pending_failures_saved_by_the_previous_version_load(self):
        """Before #119 only failures were kept, as a bare list per lesson."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pending_review.json"
            raw = {
                "format": FORMAT,
                "items": {},
                "pending_reports": {"3": ["a"], "7": []},
            }
            path.write_text(json.dumps(raw), "utf-8")
            self.assertEqual(
                ReviewQueue.load(path, D).reports(),
                [
                    (3, {"failed": ["a"], "shaky": [], "ok": []}),
                    (7, {"failed": [], "shaky": [], "ok": []}),
                ],
                "an answered-but-unreported lesson is still reported",
            )


class PlanTests(unittest.TestCase):
    def test_new_items_are_due_at_once_and_nothing_is_replaced(self):
        q = ReviewQueue()
        q.add_from_plan(plan(1, ["a"], [["a"], ["b"], ["c", "d"]]), D)
        self.assertEqual(
            set(q.entries), {"a", "b", "c+d"}, "a shared sentence stays one entry"
        )
        self.assertTrue(q.entries["a"].new)
        self.assertFalse(q.entries["b"].new)
        self.assertEqual(q.entries["a"].due, D.isoformat(), "asked at the next /lesson")
        self.assertEqual(q.entries["b"].due, (D + timedelta(days=1)).isoformat())
        q.record("a", "failed", D + timedelta(days=1))
        added = q.add_from_plan(
            plan(2, [], [["a"], ["b"], ["d"], ["e"]]), D + timedelta(days=1)
        )
        self.assertEqual(added, 1, "only e: a and b are queued, d is covered by c+d")
        self.assertEqual(
            q.entries["a"].state, "failed", "an existing entry keeps its state"
        )
        self.assertEqual(q.entries["a"].prompt, "say a")


class StorageTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pending_review.json"
            q = ReviewQueue({"a": entry("shaky", last="2026-09-25")})
            q.save(path)
            raw = json.loads(path.read_text("utf-8"))
            self.assertEqual(raw["format"], FORMAT)
            self.assertEqual(ReviewQueue.load(path, D).entries, q.entries)
            self.assertEqual(
                [p.name for p in Path(td).iterdir()], ["pending_review.json"]
            )

    def test_legacy_file_is_migrated_and_backed_up(self):
        legacy = {
            "lesson": 7,
            "questions": [
                {
                    "items": ["fyrirgefdu"],
                    "prompt": "Say: Excuse me.",
                    "answer": "Fyrirgefðu.",
                },
                {
                    "items": ["eg_vil", "fara_heim"],
                    "prompt": "帰りたい",
                    "answer": "Ég vil fara heim.",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pending_review.json"
            path.write_text(json.dumps(legacy, ensure_ascii=False), "utf-8")
            q = ReviewQueue.load(path, D)
            self.assertEqual(set(q.entries), {"fyrirgefdu", "eg_vil+fara_heim"})
            for e in q.entries.values():
                self.assertEqual(
                    (e.state, e.reviews, e.last_reviewed, e.due, e.source_lesson),
                    ("unseen", 0, None, D.isoformat(), 7),
                )
            bak = Path(td) / "pending_review.json.v1.bak"
            self.assertEqual(json.loads(bak.read_text("utf-8")), legacy)
            self.assertEqual(json.loads(path.read_text("utf-8"))["format"], FORMAT)
            self.assertEqual(
                ReviewQueue.load(path, D + timedelta(days=3)).entries,
                q.entries,
                "migrated once; later loads read the new format",
            )

    def test_missing_or_broken_file_is_an_empty_queue(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pending_review.json"
            self.assertEqual(ReviewQueue.load(path, D).entries, {})
            path.write_text("{not json", "utf-8")
            self.assertEqual(ReviewQueue.load(path, D).entries, {})
            broken = Path(td) / "pending_review.json.broken"
            self.assertEqual(
                broken.read_text("utf-8"), "{not json", "set aside, not overwritten"
            )


if __name__ == "__main__":
    unittest.main()


def bonus_plan(n, bonus, review=()):
    p = plan(n, [], review)
    p["review"] += [
        {
            "items": items,
            "prompt": f"cue {'+'.join(items)}",
            "answer": "A.",
            "bonus": True,
        }
        for items in bonus
    ]
    return p


class BonusTests(unittest.TestCase):
    """language-learning-audio #183: a line the learner only tried is a bonus question: a 言えた gains,
    a miss costs nothing, asked once, never blocks the next lesson."""

    def test_a_bonus_question_is_its_own_entry_due_tomorrow_and_never_blocks(self):
        q = ReviewQueue()
        q.add_from_plan(plan(1, [], [["a"]]), D)
        added = q.add_from_plan(bonus_plan(2, [["a", "b"]]), D)
        self.assertEqual(added, 1)
        (key,) = [k for k, e in q.entries.items() if e.bonus]
        e = q.entries[key]
        self.assertEqual(
            (e.items, e.due, e.new),
            (["a", "b"], (D + timedelta(days=1)).isoformat(), False),
        )
        self.assertEqual(q.must_answer(D), [], "the next lesson doesn't wait for it")
        self.assertIn("a", q.entries, "the ordinary entry for «a» is untouched")

    def test_at_most_two_per_review_and_after_the_due_failed_ones_before_plain_ok(self):
        q = ReviewQueue()
        q.add_from_plan(bonus_plan(1, [["a"], ["b"], ["c"]]), D)
        self.assertEqual(
            sum(e.bonus for e in q.entries.values()), 2, "at most two are added"
        )
        q.entries["f"] = entry("failed", due=D, items=["f"])
        q.entries["k"] = entry("ok", due=D, items=["k"])
        keys = q.select(D + timedelta(days=1))
        bonus = [k for k in keys if q.entries[k].bonus]
        self.assertEqual(keys[0], "f")
        self.assertLess(keys.index(bonus[0]), keys.index("k"))
        self.assertLessEqual(len(bonus), 2)
        for e in q.entries.values():
            if e.bonus:
                self.assertEqual(e.tier(D + timedelta(days=1)), 3)

    def test_said_is_reported_and_a_miss_is_not_and_the_entry_goes_either_way(self):
        q = ReviewQueue()
        q.add_from_plan(bonus_plan(3, [["a", "b"], ["c"]]), D)
        k_said, k_miss = sorted(k for k, e in q.entries.items() if e.bonus)
        q.record_bonus(k_said, "ok")
        q.record_bonus(k_miss, "failed")
        self.assertEqual(q.entries, {})
        self.assertEqual(
            q.reports(), [(3, {"failed": [], "shaky": [], "ok": ["a", "b"]})]
        )
        q2 = ReviewQueue()
        q2.add_from_plan(bonus_plan(3, [["c"]]), D)
        (k,) = q2.entries
        q2.record_bonus(k, "shaky")
        self.assertEqual(
            (q2.entries, q2.reports()), ({}, []), "a 迷った is not reported either"
        )

    def test_an_unanswered_bonus_question_is_dropped_when_a_new_plan_arrives(self):
        q = ReviewQueue()
        q.add_from_plan(bonus_plan(1, [["a"]]), D)
        q.add_from_plan(bonus_plan(2, [["b"]]), D + timedelta(days=1))
        self.assertEqual([e.items for e in q.entries.values() if e.bonus], [["b"]])

    def test_plans_without_bonus_behave_as_before_and_the_entry_round_trips(self):
        q = ReviewQueue()
        q.add_from_plan(plan(1, ["a"], [["a"]]), D)
        self.assertFalse(any(e.bonus for e in q.entries.values()))
        q.add_from_plan(bonus_plan(2, [["x"]]), D)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "q.json"
            q.save(p)
            again = ReviewQueue.load(p, D)
        self.assertEqual(
            {k: e.bonus for k, e in again.entries.items()},
            {k: e.bonus for k, e in q.entries.items()},
        )


def stored(items, answer, prompt="p", due=D, state="ok", lesson=20, open_=False):
    return Entry(items=items, prompt=prompt, answer=answer, source_lesson=lesson, state=state, due=due.isoformat(), open=open_)


class RefinementTests(unittest.TestCase):
    """site_update_notifier#96 (language-learning-audio #239): the queue keeps questions from earlier plans, so a part's bare question has to leave it
    when the plan stops asking it, and the whole's question has to be there."""

    def old_queue(self):
        q = ReviewQueue()
        q.entries = {
            "hjalpina": stored(["hjalpina"], "hjálpina", "for the help"),
            "matinn": stored(["matinn"], "matinn", "for the meal", state="failed", due=D, open_=True),
            "tvo_fullordna": stored(["tvo_fullordna"], "tvo fullorðna", "two adults", due=D + timedelta(days=3)),
            "partei_takk": stored(["partei_takk"], "Tvo fullorðna, takk.", "Two adults, please.", due=D + timedelta(days=7)),
            "eigdu_godan_dag": stored(["eigdu_godan_dag"], "Eigðu góðan dag.", "Wish him a good day."),
            "eigdu_godur": stored(["eigdu_godur"], "Eigðu góðan dag.", "Have a good day.", due=D + timedelta(days=5)),
        }
        return q

    def lesson21(self):
        return {
            "lesson_number": 21,
            "new_items": [],
            "open_items": ["matinn"],
            "review": [
                {"items": ["hjalpina"], "prompt": "Say in Icelandic: Thanks for the help.", "answer": "Takk fyrir hjálpina."},
                {"items": ["matinn"], "prompt": "Say in Icelandic: Thanks for the meal.", "answer": "Takk fyrir matinn."},
                {"items": ["partei_takk", "tvo_fullordna"], "prompt": "Two adults, please.", "answer": "Tvo fullorðna, takk."},
                {"items": ["eigdu_godan_dag", "eigdu_godur"], "prompt": "Wish him a good day.", "answer": "Eigðu góðan dag."},
            ],
            "review_refined": [
                {"items": ["hjalpina"], "kind": "through_whole", "was": "hjálpina", "now": "Takk fyrir hjálpina."},
                {"items": ["matinn"], "kind": "through_whole", "was": "matinn", "now": "Takk fyrir matinn."},
                {"items": ["tvo_fullordna"], "kind": "beside_whole", "answer": "tvo fullorðna", "whole": "Tvo fullorðna, takk."},
                {"items": ["eigdu_godur"], "kind": "same_answer", "answer": "Eigðu góðan dag."},
            ],
        }

    def test_the_replay_of_lesson_21_leaves_no_bare_part_and_one_answer_each(self):
        q = self.old_queue()
        q.add_from_plan(self.lesson21(), D)
        answers = [e.answer for e in q.entries.values()]
        self.assertEqual(len(answers), len(set(answers)), "one answer, one question")
        self.assertNotIn("matinn", answers)
        self.assertNotIn("hjálpina", answers)
        self.assertNotIn("tvo fullorðna", answers)
        self.assertEqual(answers.count("Eigðu góðan dag."), 1)
        self.assertEqual(q.entries["matinn"].answer, "Takk fyrir matinn.", "the open item is asked through its sentence")
        self.assertTrue(q.entries["matinn"].open)

    def test_the_whole_takes_the_dropped_questions_due_and_the_part_is_credited_to_it(self):
        q = self.old_queue()
        q.add_from_plan(self.lesson21(), D)
        cover = q.entries["partei_takk+tvo_fullordna"]
        self.assertEqual((cover.items, cover.answer), (["partei_takk", "tvo_fullordna"], "Tvo fullorðna, takk."))
        self.assertEqual(cover.due, (D + timedelta(days=3)).isoformat(), "the earlier due of the two")
        self.assertNotIn("tvo_fullordna", q.entries)
        both = q.entries["eigdu_godan_dag+eigdu_godur"]
        self.assertEqual(both.due, D.isoformat(), "the same answer asked twice becomes one, due at the earlier date")

    def test_an_open_part_is_asked_through_the_whole_that_took_its_question(self):
        q = ReviewQueue()
        q.entries = {
            "tvo_fullordna": stored(["tvo_fullordna"], "tvo fullorðna", state="failed", due=D, open_=True),
            "partei_takk": stored(["partei_takk"], "Tvo fullorðna, takk.", due=D + timedelta(days=7)),
        }
        plan_ = {
            "lesson_number": 21, "new_items": [], "open_items": ["tvo_fullordna"],
            "review": [{"items": ["partei_takk", "tvo_fullordna"], "prompt": "Two adults, please.", "answer": "Tvo fullorðna, takk."}],
            "review_refined": [{"items": ["tvo_fullordna"], "kind": "beside_whole", "answer": "tvo fullorðna", "whole": "Tvo fullorðna, takk."}],
        }
        q.add_from_plan(plan_, D)
        self.assertEqual(list(q.entries), ["partei_takk+tvo_fullordna"])
        cover = q.entries["partei_takk+tvo_fullordna"]
        self.assertTrue(cover.open)
        self.assertEqual(cover.due, D.isoformat(), "brought forward by the open part")
        self.assertEqual(q.must_answer(D), ["partei_takk+tvo_fullordna"])

    def test_a_part_with_no_home_leaves_the_queue_and_bonus_questions_stay(self):
        q = ReviewQueue()
        q.entries = {"hundrad": stored(["hundrad"], "hundrað"), "bonus:2:x": Entry(["x"], "p", "a", 2, due=D.isoformat(), bonus=True)}
        q.add_from_plan({"lesson_number": 2, "new_items": [], "review": [], "review_refined": [{"items": ["hundrad"], "kind": "no_home", "answer": "hundrað"}]}, D)
        self.assertEqual(list(q.entries), ["bonus:2:x"])

    def test_a_question_whose_cue_is_its_answer_leaves_the_queue(self):
        """language-learning-audio #277: «Say: Akureyri. → Akureyri» asks nothing."""
        q = ReviewQueue()
        q.entries = {"akureyri": stored(["akureyri"], "Akureyri")}
        q.add_from_plan({"lesson_number": 2, "new_items": [], "review": [], "review_refined": [{"items": ["akureyri"], "kind": "trivial", "answer": "Akureyri"}]}, D)
        self.assertEqual(list(q.entries), [])

    def test_through_whole_keeps_the_key_and_the_schedule(self):
        q = self.old_queue()
        before = q.entries["hjalpina"]
        due, state = before.due, before.state
        q.add_from_plan(self.lesson21(), D)
        self.assertEqual((q.entries["hjalpina"].answer, q.entries["hjalpina"].due, q.entries["hjalpina"].state), ("Takk fyrir hjálpina.", due, state))

    def test_one_pass_over_the_existing_queue_applies_what_refine_review_returns(self):
        """The one-time pass (`Lessons.refine_queue`): audiolesson refine-review gives the refined questions and the report; the queue is rewritten from them."""
        q = self.old_queue()
        refined = self.lesson21()
        n = q.apply_refinement(refined["review"], refined["review_refined"], D, rewrite=True)
        self.assertGreater(n, 0)
        answers = [e.answer for e in q.entries.values()]
        self.assertEqual(len(answers), len(set(answers)))
        self.assertEqual(q.entries["hjalpina"].prompt, "Say in Icelandic: Thanks for the help.", "reworded by the refined question")
        self.assertEqual(q.entries["matinn"].answer, "Takk fyrir matinn.")
        # nothing to do the second time
        self.assertEqual(q.apply_refinement(refined["review"], refined["review_refined"], D, rewrite=True), 0)

    def test_the_refined_version_is_saved(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "q.json"
            q = self.old_queue()
            q.refined = 1
            q.save(path)
            self.assertEqual(ReviewQueue.load(path, D).refined, 1)
            self.assertEqual(ReviewQueue().refined, 0)



class RetentionShareTests(unittest.TestCase):
    """language-learning-audio #220: a third of the day's questions go to due older items, and the backlog is bounded."""

    def queue(self):
        q = ReviewQueue()
        # twelve fresh failures due today (tier 1) that would fill every seat, and four old ok entries due (tier 4)
        for n in range(12):
            q.entries[f"f{n}"] = Entry(items=[f"f{n}"], prompt="p", answer=f"a{n}", source_lesson=5, state="failed", due=D.isoformat(), last_reviewed=(D - timedelta(days=1)).isoformat(), reviews=1)
        for n in range(4):
            q.entries[f"o{n}"] = Entry(items=[f"o{n}"], prompt="p", answer=f"b{n}", source_lesson=2, state="ok", due=(D - timedelta(days=2)).isoformat(), last_reviewed=(D - timedelta(days=20 - n)).isoformat(), reviews=1, streak=1)
        return q

    def test_a_third_of_the_seats_go_to_old_items_even_when_failures_would_fill_them(self):
        q = self.queue()
        keys = q.select(D, limit=9)
        self.assertEqual(len(keys), 9)
        old = [k for k in keys if k.startswith("o")]
        self.assertEqual(sorted(old), ["o0", "o1", "o2"], "three seats (a third), the longest unconfirmed first")

    def test_without_old_items_the_seats_are_used_as_before(self):
        q = self.queue()
        for k in [k for k in q.entries if k.startswith("o")]:
            del q.entries[k]
        self.assertEqual(len(q.select(D, limit=9)), 9)

    def test_the_backlog_is_bounded_by_deferring_what_the_learner_has_said_most_often(self):
        q = ReviewQueue()
        for n in range(8):
            q.entries[f"t{n}"] = Entry(items=[f"t{n}"], prompt="p", answer="a", source_lesson=2, state="ok", due=(D - timedelta(days=1)).isoformat(), reviews=3 + n, streak=1 + n)
        q.entries["f"] = Entry(items=["f"], prompt="p", answer="a", source_lesson=2, state="failed", due=D.isoformat(), reviews=1)
        q.entries["n"] = Entry(items=["n"], prompt="p", answer="a", source_lesson=2, state="unseen", new=True, due=D.isoformat())
        moved = q.defer_backlog(D, maximum=6)
        self.assertEqual(sorted(moved), ["t4", "t5", "t6", "t7"], "the most-confirmed first")
        self.assertEqual(q.due_count(D), 6)
        for k in moved:
            self.assertGreaterEqual(date.fromisoformat(q.entries[k].due), D + timedelta(days=14))
        self.assertEqual((q.entries["t7"].streak, q.entries["t7"].reviews), (8, 10), "the history stays")
        self.assertEqual(len({q.entries[k].due for k in moved}), 4, "staggered, not all back on one day")
        self.assertIn("f", q.entries)
        self.assertIn("n", q.entries)
        self.assertEqual(q.defer_backlog(D, maximum=6), [])

    def test_a_first_failure_or_hesitation_stays_at_one_day_and_an_ok_after_it_does_too(self):
        q = ReviewQueue({"f": entry(), "s": entry(), "r": entry()})
        q.record("f", "failed", D)
        q.record("s", "shaky", D)
        self.assertEqual((q.entries["f"].due, q.entries["s"].due), ((D + timedelta(days=1)).isoformat(),) * 2)
        q.record("r", "failed", D)
        q.record("r", "ok", D + timedelta(days=1))
        self.assertEqual(q.entries["r"].due, (D + timedelta(days=2)).isoformat(), "an ok after a failure is not a first answer: 1 day")

    def test_the_backlog_also_defers_what_was_answered_once_the_most_recent_first(self):
        """#133: 151 due, 109 of them answered «言えた» once: the bound can come back under the maximum."""
        q = ReviewQueue()
        day = lambda n: (D - timedelta(days=n)).isoformat()
        for n in range(10):  # answered once, the next-day check: n days ago
            q.entries[f"o{n}"] = Entry(items=[f"o{n}"], prompt="p", answer="a", source_lesson=2, state="ok", due=day(1), reviews=1, streak=1, last_reviewed=day(n + 1), new=True)  # as real ones are: they came in as a new item's next-day check
        for n in range(2):
            q.entries[f"t{n}"] = Entry(items=[f"t{n}"], prompt="p", answer="a", source_lesson=2, state="ok", due=day(1), reviews=4, streak=4, last_reviewed=day(5))
        q.entries["f"] = Entry(items=["f"], prompt="p", answer="a", source_lesson=2, state="failed", due=day(1), reviews=1)
        q.entries["n"] = Entry(items=["n"], prompt="p", answer="a", source_lesson=2, state="unseen", new=True, due=day(1))
        q.entries["op"] = Entry(items=["op"], prompt="p", answer="a", source_lesson=2, state="ok", due=day(1), reviews=1, last_reviewed=day(1), open=True)
        total = q.due_count(D)
        self.assertEqual(total, 15)
        moved = q.defer_backlog(D, maximum=8)
        self.assertEqual(q.due_count(D), 8)
        self.assertEqual(sorted(moved[:2]), ["t0", "t1"], "the twice-confirmed first")
        once = [k for k in moved if k.startswith("o")]
        self.assertEqual(sorted(once), ["o0", "o1", "o2", "o3", "o4"], "then the answered-once, the most recent answer first; the oldest stay due")
        for k in ("f", "n", "op"):
            self.assertLessEqual(q.entries[k].due, D.isoformat(), f"{k} stays due")
        for k in moved:
            self.assertGreaterEqual(date.fromisoformat(q.entries[k].due), D + timedelta(days=14))

    def test_a_queue_shaped_like_lesson_25s_comes_back_to_the_maximum(self):
        """#134 review: entries enter as a new item's next-day check (new=True, never cleared); answered ok, they are what the bound defers."""
        q = ReviewQueue()
        past = (D - timedelta(days=1)).isoformat()
        for n in range(109):
            q.entries[f"a{n}"] = Entry(items=[f"a{n}"], prompt="p", answer="a", source_lesson=2, state="ok", due=past, reviews=1, streak=1, new=True, last_reviewed=(D - timedelta(days=2 + n % 10)).isoformat())
        for n in range(18):
            q.entries[f"b{n}"] = Entry(items=[f"b{n}"], prompt="p", answer="a", source_lesson=2, state="ok", due=past, reviews=2, streak=2, new=True)
        for n in range(13):
            q.entries[f"s{n}"] = Entry(items=[f"s{n}"], prompt="p", answer="a", source_lesson=2, state="shaky", due=past, reviews=1)
        for n in range(9):
            q.entries[f"n{n}"] = Entry(items=[f"n{n}"], prompt="p", answer="a", source_lesson=3, state="unseen", new=True, due=past)
        for n in range(2):
            q.entries[f"u{n}"] = Entry(items=[f"u{n}"], prompt="p", answer="a", source_lesson=3, state="unseen", due=past)
        self.assertEqual(q.due_count(D), 151)
        q.defer_backlog(D)
        self.assertEqual(q.due_count(D), 100)
        self.assertEqual(q.backlog_parts(D)["新出"], 9, "the new-item checks stay due")

    def test_a_deferred_entry_is_not_re_added_as_a_new_question_by_the_next_plan(self):
        q = ReviewQueue()
        for n in range(4):
            q.entries[f"t{n}"] = Entry(items=[f"t{n}"], prompt="p", answer="a", source_lesson=2, state="ok", due=(D - timedelta(days=1)).isoformat(), reviews=4, streak=2 + n)
        q.defer_backlog(D, maximum=2)
        q.add_from_plan({"lesson_number": 9, "new_items": [], "review": [{"items": ["t3"], "prompt": "p2", "answer": "a2", "stage": "meaning"}]}, D + timedelta(days=1))
        self.assertEqual((q.entries["t3"].streak, q.entries["t3"].reviews, q.entries["t3"].state), (5, 4, "ok"))
        self.assertEqual(sum(1 for k in q.entries if k == "t3"), 1)

    def test_the_retention_seats_go_to_ok_and_shaky_entries_not_to_failures_or_open_ones(self):
        q = self.queue()
        q.entries["o0"].state = "failed"  # a failure is first by tier (its old due date puts it in the ordinary seats); it does not use a retention seat
        keys = q.select(D, limit=9)
        self.assertEqual([k for k in keys if k.startswith("o") and q.entries[k].state == "ok"], ["o1", "o2", "o3"], "the three retention seats go to ok entries, oldest check first")
        q.entries["o3"].open = True
        q.entries["o2"].state = "failed"
        self.assertEqual([k for k in q.select(D, limit=9) if k in ("o1", "o2", "o3") and q.entries[k].state == "ok" and not q.entries[k].open], ["o1"])


class GapReportTests(unittest.TestCase):
    """language-learning-audio #220: an answer is reported with the days since the question was last confirmed."""

    def test_an_answer_after_a_gap_records_it_per_item_and_a_short_gap_does_not(self):
        q = ReviewQueue()
        q.entries["ab"] = Entry(items=["a", "b"], prompt="p", answer="x", source_lesson=4, state="ok", due=D.isoformat(), last_reviewed=(D - timedelta(days=14)).isoformat(), reviews=2, streak=2)
        q.entries["c"] = Entry(items=["c"], prompt="p", answer="y", source_lesson=4, state="ok", due=D.isoformat(), last_reviewed=(D - timedelta(days=1)).isoformat(), reviews=1)
        q.entries["d"] = Entry(items=["d"], prompt="p", answer="z", source_lesson=4, state="unseen", due=D.isoformat())
        for k in ("ab", "c", "d"):
            q.record(k, "ok", D)
        self.assertEqual(q.pending_gaps, {4: {"a": 14, "b": 14}})

    def test_the_gaps_are_saved_loaded_and_cleared_when_the_report_is_sent(self):
        q = ReviewQueue()
        q.entries["a"] = Entry(items=["a"], prompt="p", answer="x", source_lesson=4, state="ok", due=D.isoformat(), last_reviewed=(D - timedelta(days=7)).isoformat(), reviews=2)
        q.record("a", "ok", D)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "q.json"
            q.save(path)
            loaded = ReviewQueue.load(path, D)
            self.assertEqual(loaded.pending_gaps, {4: {"a": 7}})
            for lesson, report in loaded.reports():
                loaded.mark_reported(lesson, report)
            self.assertEqual((loaded.pending_gaps, loaded.reports()), ({}, []))

    def test_report_args_carry_the_gaps(self):
        from src.lesson import LessonConfig

        cfg = LessonConfig(root=Path("/x"), users={1: "yuki"}, default_minutes=5, default_new_list="after", default_order="spread")
        args = cfg.report_args("yuki", [], lesson=4, recalled=["a", "b"], gaps={"a": 14, "b": 7})
        self.assertEqual(args[args.index("--gaps") + 1], "a=14,b=7")
        self.assertNotIn("--gaps", cfg.report_args("yuki", [], lesson=4, recalled=["a"]))
