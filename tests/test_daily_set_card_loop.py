"""Regression tests for the Daily Set card loop.

Covers issue #131: the Daily Set reported "only completing 2/3". The loop used to
click cards by a positional index captured before the first click, but the
Rewards page re-renders as cards complete and can drop finished cards from the
DOM. Every index after the first then pointed at the wrong card — or past the
end — so one card was silently skipped.

These tests drive `DailySet._process_section` with a fake driver that models the
two ways the real page presents a finished card:

  * still rendered, but showing as COMPLETE (the usual case), and
  * removed from the DOM entirely (`vanish_on_complete`).

Both must produce the same result: every actionable card clicked once, and the
section reported as fully complete.
"""

import unittest

from src.dailytasks.card_js import CardStatus
from src.dailytasks.runner import DailySet

STATUS_FILE = "/tmp/autorewarder-test-status-unused.json"


class FakeCard:
    """Stand-in for a Selenium element holding only what the loop touches."""

    def __init__(self, title):
        self.title = title
        self.completed = False
        self.clicked = 0

    def __repr__(self):
        return f"FakeCard({self.title!r}, completed={self.completed})"


class FakeCards:
    """Minimal RewardsCard: classify by completion, read titles, record clicks."""

    def is_visible(self, card):
        return True

    def classify(self, card, section_name=None):
        return CardStatus.COMPLETE if card.completed else CardStatus.INCOMPLETE

    def get_title(self, card):
        return card.title

    def click(self, card, human, main_tab, label="", stop_event=None):
        card.clicked += 1
        card.completed = True
        return True


class FakeDriver:
    """Renders the section the way the live page does after a card completes."""

    def __init__(self, cards, vanish_on_complete=False):
        self.all_cards = cards
        self.vanish_on_complete = vanish_on_complete

    def find_elements(self, by, selector):
        if self.vanish_on_complete:
            return [card for card in self.all_cards if not card.completed]
        return list(self.all_cards)

    def execute_script(self, script, *args):
        return None


class FakeHuman:
    pass


class DailySetCardLoopTests(unittest.TestCase):
    def _run(self, titles, vanish_on_complete=False, pre_completed=()):
        cards = [FakeCard(title) for title in titles]
        for index in pre_completed:
            cards[index].completed = True
        driver = FakeDriver(cards, vanish_on_complete=vanish_on_complete)
        daily = DailySet(status_file=STATUS_FILE)
        daily.cards = FakeCards()
        result = daily._process_section(
            driver,
            FakeHuman(),
            "Daily Set",
            "cards",
            "main-tab",
            stop_event=None,
        )
        return cards, result

    def test_every_card_is_clicked_when_completed_cards_stay_rendered(self):
        cards, result = self._run(["Card A", "Card B", "Card C"])

        self.assertEqual([card.clicked for card in cards], [1, 1, 1])
        self.assertEqual(result["attempted"], 3)
        self.assertEqual(result["newly"], 3)
        self.assertEqual(result["final"], result["total"])
        self.assertEqual(result["total"], 3)

    def test_every_card_is_clicked_when_the_dom_shrinks(self):
        """The exact reported symptom: 2 of 3 completed, one silently skipped."""
        cards, result = self._run(["Card A", "Card B", "Card C"], True)

        clicked = [card.title for card in cards if card.clicked]
        self.assertEqual(
            sorted(clicked),
            ["Card A", "Card B", "Card C"],
            "every actionable card must be clicked even as the DOM shrinks",
        )
        self.assertEqual(result["attempted"], 3)
        self.assertEqual(result["newly"], 3)
        self.assertEqual(result["final"], result["total"])

    def test_cards_are_clicked_exactly_once(self):
        for vanish in (False, True):
            cards, _ = self._run(["Card A", "Card B", "Card C"], vanish)
            self.assertTrue(
                all(card.clicked == 1 for card in cards),
                f"vanish_on_complete={vanish} clicked {[c.clicked for c in cards]}",
            )

    def test_already_complete_cards_are_not_clicked(self):
        cards, result = self._run(["Done", "Todo A", "Todo B"], pre_completed=(0,))

        self.assertEqual(cards[0].clicked, 0)
        self.assertEqual(cards[1].clicked, 1)
        self.assertEqual(cards[2].clicked, 1)
        self.assertEqual(result["already"], 1)
        self.assertEqual(result["newly"], 2)
        self.assertEqual(result["total"], 3)

    def test_duplicate_titles_are_each_clicked(self):
        """Two cards may share a title; matching by title must not merge them."""
        cards, result = self._run(["Same title", "Same title", "Card C"])
        self.assertEqual(result["attempted"], 3)
        self.assertEqual([card.clicked for card in cards], [1, 1, 1])
        self.assertEqual(result["newly"], 3)


if __name__ == "__main__":
    unittest.main()
