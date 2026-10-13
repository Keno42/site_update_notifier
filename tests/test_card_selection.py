"""#132: a new card in every review while the deck has unseen ones."""
import unittest
from datetime import date, timedelta

from src.cards import CardQueue, CardState

D = date(2026, 10, 14)  # an even ordinal? checked in the test, not assumed


def deck(n):
    return [{"id": f"c{k}"} for k in range(n)]


def queue(due_ids):
    return CardQueue({k: CardState(state="ok", streak=1, reviews=1, last_reviewed=(D - timedelta(days=2)).isoformat(), due=(D - timedelta(days=1)).isoformat()) for k in due_ids})


class SelectTests(unittest.TestCase):
    def test_with_more_due_cards_than_seats_one_seat_holds_an_unseen_card(self):
        q = queue(["c0", "c1", "c2", "c3"])
        picked = [c["id"] for c in q.select(deck(6), D, 3)]
        self.assertEqual(len(picked), 3)
        self.assertEqual(sum(1 for k in picked if k not in q.cards), 1, "one unseen card")
        self.assertEqual(picked[:2], ["c0", "c1"], "the due cards take the rest, the most urgent first")

    def test_with_no_unseen_card_all_seats_go_to_due_cards(self):
        q = queue(["c0", "c1", "c2", "c3"])
        picked = [c["id"] for c in q.select(deck(4), D, 3)]
        self.assertEqual(picked, ["c0", "c1", "c2"])

    def test_a_single_seat_alternates_between_a_new_card_and_a_due_one(self):
        q = queue(["c0", "c1"])
        days = [D + timedelta(days=k) for k in range(4)]
        picks = [q.select(deck(5), d, 1)[0]["id"] in q.cards for d in days]  # True: a due card
        self.assertEqual(sorted(set(picks)), [False, True])
        self.assertEqual([a != b for a, b in zip(picks, picks[1:])], [True, True, True], "one then the other, day by day")

    def test_when_few_cards_are_due_new_ones_fill_the_rest(self):
        q = queue(["c0"])
        picked = [c["id"] for c in q.select(deck(6), D, 3)]
        self.assertEqual(picked, ["c0", "c1", "c2"])

    def test_nothing_due_and_nothing_new_gives_nothing(self):
        self.assertEqual(CardQueue().select([], D, 3), [])


if __name__ == "__main__":
    unittest.main()
