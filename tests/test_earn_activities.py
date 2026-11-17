"""Tests for the Read to Earn / Explore on Bing / check-in pass.

These three tasks are the ones a click alone cannot complete, so what matters is
that each is driven by its own rules and that "done" is only reported when the
card's own progress says so.

Two kinds of test here:

  * classification — Read and Explore cards must be recognised, because the
    generic "More Activities" pass skips them on that basis. Getting this wrong
    means either earning nothing (skipped) or double-clicking (not skipped).
  * driving — with a fake driver standing in for the page, a card is only
    counted when its progress advances, and a card that never advances is
    reported rather than assumed.

The fake models the rules the real page follows: reading advances a counter,
Explore advances when its topics are opened, and the check-in flips a badge.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.dailytasks.earn_activities import (  # noqa: E402
    EarnActivities,
    _is_allowed_url,
    _parse_progress,
    classify_earn_kind,
)


class ClassifyEarnKindTests(unittest.TestCase):
    def test_read_to_earn_is_recognised(self):
        self.assertEqual(classify_earn_kind("Read to Earn"), "read")
        self.assertEqual(classify_earn_kind("Read and Earn"), "read")
        self.assertEqual(classify_earn_kind("Read articles to earn"), "read")

    def test_read_variants_are_recognised(self):
        self.assertEqual(classify_earn_kind("Read the latest stories"), "read")
        self.assertEqual(classify_earn_kind("Read news"), "read")

    def test_explore_on_bing_is_recognised(self):
        self.assertEqual(classify_earn_kind("Explore on Bing"), "explore")
        self.assertEqual(classify_earn_kind("Explore with Bing"), "explore")
        self.assertEqual(classify_earn_kind("Related topics"), "explore")

    def test_check_in_is_recognised(self):
        self.assertEqual(classify_earn_kind("Daily check-in"), "check_in")
        self.assertEqual(classify_earn_kind("Check in"), "check_in")

    def test_recognition_uses_the_card_text_too(self):
        self.assertEqual(
            classify_earn_kind("Earn 10 points", "Read to Earn - read an article"),
            "read",
        )
        self.assertEqual(
            classify_earn_kind("", "Explore on Bing for 5 points"), "explore"
        )

    def test_ordinary_cards_are_left_to_the_generic_pass(self):
        for title in (
            "This or that",
            "Celebrity quiz",
            "Pool: which sport?",
            "Install the Bing extension",
            "Refer a friend",
            "+30 points",
        ):
            self.assertIsNone(classify_earn_kind(title), title)


class HelperTests(unittest.TestCase):
    def test_progress_is_parsed(self):
        self.assertEqual(_parse_progress("1/3"), (1, 3))
        self.assertEqual(_parse_progress(" 0 / 2 "), (0, 2))

    def test_non_fractions_are_not_parsed(self):
        self.assertEqual(_parse_progress("+50"), (None, None))
        self.assertEqual(_parse_progress(None), (None, None))
        self.assertEqual(_parse_progress(""), (None, None))

    def test_only_expected_hosts_may_be_opened(self):
        self.assertTrue(_is_allowed_url("https://rewards.bing.com/earn"))
        self.assertTrue(_is_allowed_url("https://www.bing.com/search?q=x"))
        self.assertTrue(_is_allowed_url("https://www.msn.com/article"))

    def test_lookalike_and_unexpected_hosts_are_refused(self):
        # A card's href is page-supplied data, so it must not be trusted.
        for url in (
            "https://evil.example.com/steal",
            "https://bing.com.evil.example/x",
            "javascript:alert(1)",
            "data:text/html,hi",
            "ftp://bing.com/x",
            "",
            None,
        ):
            self.assertFalse(_is_allowed_url(url), url)


class _FakeCard:
    """A card whose progress advances only when its activity is really done."""

    def __init__(self, title, href, kind, done=0, total=1, text=""):
        self.title = title
        self.href = href
        self.kind = kind
        self.done = done
        self.total = total
        self.completed = False
        self.text = text or title


class _FakePage:
    """
    Stands in for the earn page.

    It implements just enough of the WebDriver surface the driver uses, and
    models the three behaviours that matter: reading advances a read counter,
    opening a topic advances an explore counter, and clicking the check-in marks
    it done. A card only completes when it reaches its total.
    """

    def __init__(self, cards, check_in=False, topics=None):
        self.cards = cards
        self.check_in_offered = check_in
        self.check_in_done = False
        self.topics = topics or []
        self.opened = []
        self.current_window_handle = "main"
        self.window_handles = ["main"]
        self.current_url = "https://rewards.bing.com/earn"

    def execute_script(self, script, *args):
        # The page is always hydrated in these tests; without this the readiness
        # poll would sit out its full timeout for nothing.
        if "window.__next_f" in script and "getElementById" in script:
            return True
        if "keyword.test" in script and '"statusSuccess"' in script:
            return [
                {
                    "title": card.title,
                    "text": card.text,
                    "href": card.href,
                    "progress": f"{card.done}/{card.total}" if card.total else None,
                    "completed": card.completed,
                    "points": 10,
                }
                for card in self.cards
            ]
        if "bing.com/search" in script and "seen[href]" in script:
            return [{"href": href, "title": t} for href, t in self.topics]
        # The probe is the only script that looks for the check-in control; the
        # state read is the only one that returns a done flag. Keying on those
        # keeps the fake from confusing the two, which share the word "check".
        if "card_text" in script:
            if not self.check_in_offered or self.check_in_done:
                return None
            return {
                "control": "CHECKIN",
                "via": "label",
                "card": "Daily check-in",
                "card_text": "Daily check-in",
            }
        if "done: !!" in script:
            if not self.check_in_offered:
                return None
            return {
                "done": self.check_in_done,
                "text": "claimed" if self.check_in_done else "check in",
            }
        return None

    def get(self, url):
        self.opened.append(url)
        self.current_url = url
        for card in self.cards:
            if card.href == url and card.kind == "read":
                card.done = min(card.total, card.done + 1)
                card.completed = card.done >= card.total
        if self.topics and any(url == href for href, _ in self.topics):
            for card in self.cards:
                if card.kind == "explore" and card.href not in self.opened:
                    continue
                if card.kind == "explore":
                    card.done = min(card.total, card.done + 1)
                    card.completed = card.done >= card.total

    def find_elements(self, by, selector):
        return []

    class _SwitchTo:
        """Mirrors Selenium's driver.switch_to.window(...) shape."""

        def __init__(self, page):
            self._page = page

        def window(self, handle):
            self._page.current_window_handle = handle

    @property
    def switch_to(self):
        return _FakePage._SwitchTo(self)

    def close(self):
        pass


class _FakeHuman:
    def __init__(self, page):
        self.page = page

    def click_element(self, element, scroll_into_view=True):
        if element == "CHECKIN":
            self.page.check_in_done = True

    def scroll_page(self):
        return None


class EarnDrivingTests(unittest.TestCase):
    def _run(self, cards, check_in=False, topics=None):
        page = _FakePage(cards, check_in=check_in, topics=topics)
        # No real reading time in tests: the pause is what production needs,
        # not what this suite is checking.
        activities = EarnActivities(read_seconds=(0.0, 0.01), pacing=False)
        page.current_url = "https://rewards.bing.com/earn"
        # Call the passes directly: run() would reload the page, which the fake
        # does not model.
        read = activities.run_read_to_earn(page, _FakeHuman(page))
        explore = activities.run_explore(page, _FakeHuman(page))
        claimed = activities.run_check_in(page, _FakeHuman(page))
        return page, read, explore, claimed

    def test_reading_advances_a_read_card(self):
        card = _FakeCard("Read to Earn", "https://www.msn.com/a", "read", 0, 1)
        _, read, _, _ = self._run([card])
        self.assertEqual(read, 1)
        self.assertTrue(card.completed)

    def test_a_card_needing_three_reads_is_read_three_times(self):
        card = _FakeCard("Read to Earn", "https://www.msn.com/a", "read", 0, 3)
        _, read, _, _ = self._run([card])
        self.assertEqual(read, 1)
        self.assertTrue(card.completed)
        self.assertEqual(card.done, 3)

    def test_an_already_complete_read_card_is_left_alone(self):
        card = _FakeCard("Read to Earn", "https://www.msn.com/a", "read", 1, 1)
        card.completed = True
        page, read, _, _ = self._run([card])
        self.assertEqual(read, 0)
        self.assertEqual(page.opened, [])

    def test_explore_topics_are_opened_and_the_card_completes(self):
        card = _FakeCard("Explore on Bing", "https://www.bing.com/hub", "explore", 0, 1)
        topics = [("https://www.bing.com/search?q=space", "Space")]
        page, _, explore, _ = self._run([card], topics=topics)
        self.assertIn("https://www.bing.com/search?q=space", page.opened)
        self.assertEqual(explore, 1)

    def test_check_in_is_claimed_once(self):
        _, _, _, claimed = self._run([], check_in=True)
        self.assertEqual(claimed, 1)

    def test_check_in_is_not_reported_when_not_offered(self):
        _, _, _, claimed = self._run([], check_in=False)
        self.assertEqual(claimed, 0)

    def test_explore_with_no_topics_does_not_report_success(self):
        card = _FakeCard("Explore on Bing", "https://www.bing.com/hub", "explore", 0, 1)
        page, _, explore, _ = self._run([card], topics=[])
        self.assertEqual(explore, 0)

    def test_a_card_that_does_not_advance_is_not_counted(self):
        # Reading does not move the counter (the page credits nothing), so the
        # pass must report zero rather than assume the read worked.
        class StubbornPage(_FakePage):
            def get(self, url):
                self.opened.append(url)

        page = StubbornPage([])
        card = _FakeCard("Read to Earn", "https://www.msn.com/a", "read", 0, 1)
        page.cards = [card]
        activities = EarnActivities(read_seconds=(0.0, 0.01), pacing=False)
        read = activities.run_read_to_earn(page, _FakeHuman(page))
        self.assertEqual(read, 0)


class StopTests(unittest.TestCase):
    def test_a_stop_request_ends_the_pass(self):
        import threading

        card = _FakeCard("Read to Earn", "https://www.msn.com/a", "read", 0, 3)
        page = _FakePage([card])
        stop = threading.Event()
        stop.set()

        activities = EarnActivities(pacing=False)
        read = activities.run_read_to_earn(page, _FakeHuman(page), stop_event=stop)

        self.assertEqual(read, 0)
        self.assertEqual(page.opened, [], "nothing may be opened after Stop")


if __name__ == "__main__":
    unittest.main()
