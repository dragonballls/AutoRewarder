"""What the app does when the offer list says no, and how it is wired in.

Three contracts live here, and each of them is about not fooling the person
using the app:

  * the earn passes are reached through `NewDashboardDailySet.perform`, from the
    offer list, and each surface owns its own group so nothing is counted twice;
  * a failed report falls back to the page pass *and is re-verified*, instead of
    reporting the attempt as done;
  * the run's reported totals reach the statistics as verified numbers only, and
    the dashboard's "opens" stay separate from them.
"""

import ast
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.dailytasks.earn_activities import EarnActivities  # noqa: E402
from src.emulator.driver import _is_closed_session  # noqa: E402


def _source(relative_path):
    with open(os.path.join(REPO_ROOT, relative_path), encoding="utf-8") as handle:
        return handle.read()


def _methods(class_name, path):
    tree = ast.parse(_source(path))
    klass = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return {
        node.name: ast.unparse(node)
        for node in klass.body
        if isinstance(node, ast.FunctionDef)
    }


class _FakeEarner:
    """An earner whose offer list is fixed, and whose reports move it or not."""

    def __init__(self, offers, creditable=False):
        self.view_model = {"flyoutResult": {}}
        self.offers_state = [dict(offer) for offer in offers]
        self.creditable = creditable
        self.earned = []
        self.refreshes = 0
        self.driver = None

    def load(self, navigate=True, quiet=False):
        return self.view_model

    def offers(self):
        return [dict(offer) for offer in self.offers_state]

    def refresh(self):
        self.refreshes += 1
        return self.view_model

    def earn(self, offers, stop_event=None, max_reports_per_offer=20):
        for offer in offers:
            self.earned.append(offer["offer_id"])
            if self.creditable:
                for index, current in enumerate(self.offers_state):
                    if current["offer_id"] != offer["offer_id"]:
                        continue
                    maximum = current.get("maximum") or 1
                    self.offers_state[index] = {
                        **current,
                        "progress": maximum,
                        "complete": True,
                    }
        return {
            "attempted": len(offers),
            "verified": len(offers) if self.creditable else 0,
            "reports": len(offers),
            "skipped": 0,
        }

    def session_state(self, navigate=True):
        return "signed_in"


def _offer(offer_id, kind, **extra):
    record = {
        "offer_id": offer_id,
        "hash": "hash-" + offer_id,
        "title": extra.pop("title", offer_id),
        "source": kind,
        "role": None,
        "activity_type": 11,
        "subtype": "",
        "progress": extra.pop("progress", 0),
        "maximum": extra.pop("maximum", 1),
        "complete": extra.pop("complete", False),
        "locked": False,
        "destination": "https://www.bing.com/search?q=x",
        "date": extra.pop("date", None),
    }
    record.update(extra)
    return record


class ReportingPassTests(unittest.TestCase):
    def test_read_to_earn_reports_the_offer_instead_of_reading_a_page(self):
        earner = _FakeEarner(
            [_offer("read-1", "morePromotions", title="Read to Earn")],
            creditable=True,
        )
        activities = EarnActivities(earner=earner, pacing=False)
        totals = activities.run_by_report(object())
        self.assertEqual(totals["read"], 1)
        self.assertEqual(earner.earned, ["read-1"])

    def test_a_report_that_does_not_move_falls_back_and_reverifies(self):
        """
        The case the old passes got wrong: accepted report, no credit.

        The page pass must then run, and its result must come from the offer list
        rather than from the pass's own opinion of itself.
        """
        earner = _FakeEarner(
            [_offer("read-1", "morePromotions", title="Read to Earn")],
            creditable=False,
        )
        activities = EarnActivities(earner=earner, pacing=False)
        calls = []

        def fake_page_pass(driver, human, stop_event=None):
            calls.append("page")
            # The page pass credits it, so the re-read must see it complete.
            earner.offers_state[0] = {
                **earner.offers_state[0],
                "progress": 1,
                "complete": True,
            }
            return 1

        activities.run_read_to_earn = fake_page_pass
        totals = activities.run_by_report(object(), human=object())

        self.assertEqual(calls, ["page"])
        self.assertEqual(totals["read"], 1)
        self.assertGreaterEqual(earner.refreshes, 1)

    def test_a_fallback_that_credits_nothing_is_not_counted(self):
        earner = _FakeEarner(
            [_offer("read-1", "morePromotions", title="Read to Earn")],
            creditable=False,
        )
        activities = EarnActivities(earner=earner, pacing=False)
        activities.run_read_to_earn = lambda *a, **k: 1
        totals = activities.run_by_report(object(), human=object())
        self.assertEqual(totals["read"], 0)

    def test_an_offer_the_list_says_is_done_is_left_alone(self):
        earner = _FakeEarner(
            [
                _offer(
                    "read-1",
                    "morePromotions",
                    title="Read to Earn",
                    progress=1,
                    maximum=1,
                    complete=True,
                )
            ]
        )
        activities = EarnActivities(earner=earner, pacing=False)
        totals = activities.run_by_report(object())
        self.assertEqual(totals["read"], 0)
        self.assertEqual(earner.earned, [])

    def test_the_check_in_is_a_report_not_a_button_hunt(self):
        earner = _FakeEarner(
            [_offer("checkin-1", "dailyCheckInPromotion")], creditable=True
        )
        activities = EarnActivities(earner=earner, pacing=False)
        totals = activities.run_by_report(object())
        self.assertEqual(totals["check_in"], 1)
        self.assertEqual(earner.earned, ["checkin-1"])

    def test_without_an_earner_the_page_pass_still_runs(self):
        """No earner must not mean "do nothing": the fallback path is the app."""
        source = _methods("EarnActivities", "src/dailytasks/earn_activities.py")["run"]
        self.assertIn("run_by_report", source)
        self.assertIn("_open_earn_page", source)


class WiringTests(unittest.TestCase):
    def test_the_daily_set_earns_through_the_offer_list_first(self):
        daily = _methods("NewDashboardDailySet", "src/dailytasks/new_dashboard.py")[
            "_run_daily_set"
        ]
        self.assertIn("_load_offers", daily)
        self.assertIn("_run_daily_set_by_report", daily)
        # And keeps the page path for a site that cannot be read.
        self.assertIn("_run_daily_set_by_click", daily)

    def test_more_activities_and_quests_report_before_clicking(self):
        methods = _methods("NewDashboardDailySet", "src/dailytasks/new_dashboard.py")
        more = methods["_run_more_activities"]
        self.assertIn("_run_more_activities_by_report", more)
        self.assertIn("_run_more_activities_by_click", more)
        quests = methods["_run_quests"]
        self.assertIn("_run_quests_by_report", quests)
        self.assertIn("_run_quests_by_click", quests)

    def test_each_surface_owns_one_group(self):
        """Two passes reporting the same offer would count it twice."""
        source = _source("src/dailytasks/new_dashboard.py")
        self.assertIn('DAILY_SET_SOURCE = "dailySetPromotions"', source)
        self.assertIn('QUEST_SOURCE = "punchCards"', source)
        selection = _methods("NewDashboardDailySet", "src/dailytasks/new_dashboard.py")[
            "_more_activity_offers"
        ]
        self.assertIn("DAILY_SET_SOURCE", selection)
        self.assertIn("QUEST_SOURCE", selection)

    def test_the_earn_page_activities_are_excluded_from_more_activities(self):
        selection = _methods("NewDashboardDailySet", "src/dailytasks/new_dashboard.py")[
            "_more_activity_offers"
        ]
        self.assertIn("offer_earn_kind", selection)

    def test_the_report_counts_are_folded_in_once(self):
        perform = _methods("NewDashboardDailySet", "src/dailytasks/new_dashboard.py")[
            "perform"
        ]
        self.assertIn("last_reported", perform)
        for pass_name in (
            "_run_daily_set",
            "_run_claim",
            "_run_more_activities",
            "_run_quests",
        ):
            self.assertIn(pass_name, perform)

    def test_the_earn_runner_hands_the_earner_over(self):
        method = _methods("DailySet", "src/dailytasks/runner.py")["run_earn_activities"]
        self.assertIn("RewardsApiEarner", method)
        self.assertIn("earner=earner", method)

    def test_the_earner_is_built_on_the_same_driver(self):
        method = _methods("NewDashboardDailySet", "src/dailytasks/new_dashboard.py")[
            "_earner"
        ]
        self.assertIn("earner.driver is not driver", method)


class ClosedSessionTests(unittest.TestCase):
    """
    Closing the browser is how a person says First Setup is finished. Every
    Selenium message for that must read as "finished", not as a crash — the
    "invalid session id" backtrace in the report was one of them.
    """

    def test_the_messages_selenium_uses_for_a_closed_browser_are_recognised(self):
        for message in (
            "Message: invalid session id",
            "Message: target window already closed",
            "Message: no such window: target window already closed",
            "disconnected: not connected to DevTools",
            "chrome not reachable",
            "session deleted because of page crash",
        ):
            self.assertTrue(_is_closed_session(message), message)

    def test_a_real_failure_is_not_mistaken_for_a_close(self):
        for message in (
            "Message: element not interactable",
            "Message: stale element reference",
            "Could not reach the sign-in page",
        ):
            self.assertFalse(_is_closed_session(message), message)


class SignedInTests(unittest.TestCase):
    def test_first_setup_verifies_the_sign_in_instead_of_trusting_a_close(self):
        source = _source("src/api.py")
        start = source.index("def _run_first_setup_for_current")
        end = source.index("def get_history", start)
        body = source[start:end]
        self.assertIn("_verify_signed_in", body)
        # And a failed check must not record the setup as done.
        self.assertIn("setup_succeeded = False", body)

    def test_a_closed_window_alone_is_not_success(self):
        """`while len(window_handles) > 0` used to end with setup_succeeded=True."""
        source = _source("src/api.py")
        start = source.index("def _run_first_setup_for_current")
        end = source.index("def get_history", start)
        body = source[start:end]
        self.assertIn("_verify_signed_in", body)

    def test_a_balance_comes_from_the_offer_list_too(self):
        method = _methods("AutoRewarderAPI", "src/api.py")["_fetch_balance_with_driver"]
        self.assertIn("RewardsApiEarner", method)
        self.assertIn("available_points", method)


if __name__ == "__main__":
    unittest.main()
