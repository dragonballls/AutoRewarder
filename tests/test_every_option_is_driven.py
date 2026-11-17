"""Every Microsoft Rewards earning option must actually be driven.

This is the contract behind "make it do all of it": each earning surface on the
dashboard and the earn page has exactly one owner, and every owner is wired into
a run. A surface with no owner would simply never be attempted, and the app
would look complete while quietly leaving points on the table.

The test reads the orchestration code rather than trusting a description, so
removing a pass (or wiring it to nothing) fails here.
"""

import ast
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _source(path):
    with open(os.path.join(REPO_ROOT, path), encoding="utf-8") as handle:
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


class EveryEarningOptionIsDrivenTests(unittest.TestCase):
    """The five earning surfaces, each with the pass that drives it."""

    def test_daily_set_is_driven(self):
        perform = _methods("NewDashboardDailySet", "src/dailytasks/new_dashboard.py")[
            "perform"
        ]
        self.assertIn("_run_daily_set", perform)
        self.assertIn("_run_daily_set", _source("src/dailytasks/new_dashboard.py"))

    def test_more_activities_is_driven(self):
        perform = _methods("NewDashboardDailySet", "src/dailytasks/new_dashboard.py")[
            "perform"
        ]
        self.assertIn("_run_more_activities", perform)

    def test_quests_are_driven(self):
        perform = _methods("NewDashboardDailySet", "src/dailytasks/new_dashboard.py")[
            "perform"
        ]
        self.assertIn("_run_quests", perform)

    def test_pending_points_are_claimed(self):
        perform = _methods("NewDashboardDailySet", "src/dailytasks/new_dashboard.py")[
            "perform"
        ]
        self.assertIn("_run_claim", perform)

    def test_read_to_earn_explore_and_check_in_are_driven(self):
        run = _methods("EarnActivities", "src/dailytasks/earn_activities.py")["run"]
        for pass_name in ("run_read_to_earn", "run_explore", "run_check_in"):
            self.assertIn(pass_name, run, f"{pass_name} is not called by run()")

    def test_the_earn_pass_is_wired_into_a_run(self):
        """Both the normal run and the daily-tasks-only run must call it."""
        api = _source("src/api.py")
        self.assertIn("def _run_earn_activities", api)
        # Called from the daily-tasks-only path and the phase path, including
        # the already-done branches.
        self.assertGreaterEqual(
            api.count("self._run_earn_activities("),
            4,
            "the earn pass must be called from every path that runs daily tasks",
        )

    def test_the_earn_pass_asks_the_daily_set_to_run_it(self):
        methods = _methods("DailySet", "src/dailytasks/runner.py")
        self.assertIn("run_earn_activities", methods)
        self.assertIn("EarnActivities", methods["run_earn_activities"])

    def test_read_and_explore_are_not_also_opened_by_the_generic_pass(self):
        """
        Read/Explore cards live in the same section as the ordinary cards. The
        generic pass must skip them, or they would be opened for a few seconds
        (earning nothing) and counted as done before the real pass runs.

        Both paths are checked: the report path skips them by kind, and the
        click path skips them by the same classifier.
        """
        methods = _methods("NewDashboardDailySet", "src/dailytasks/new_dashboard.py")
        report = methods["_run_more_activities_by_report"]
        self.assertIn("_more_activity_offers", report)
        selection = methods["_more_activity_offers"]
        self.assertIn("offer_earn_kind", selection)
        self.assertIn("is_reportable", selection)

        more = methods["_run_more_activities_by_click"]
        self.assertIn("classify_earn_kind", more)
        # ast.unparse normalises quotes, so the tuple reads with either style.
        self.assertIn("read", more)
        self.assertIn("explore", more)
        self.assertIn("handled by the earn-activities pass", more)

    def test_visual_search_is_still_driven(self):
        phase = _methods("AutoRewarderAPI", "src/api.py")["_run_phase"]
        self.assertIn("_run_visual_search_if_needed", phase)

    def test_searches_are_still_driven(self):
        phase = _methods("AutoRewarderAPI", "src/api.py")["_run_phase"]
        self.assertIn("perform_searches", phase)


class NoSurfaceIsClaimedTwiceTests(unittest.TestCase):
    def test_earn_pass_only_counts_verified_completions(self):
        """A count must only rise when the card's own state says so."""
        source = _source("src/dailytasks/earn_activities.py")
        # The read pass increments only after a verified look at the card.
        self.assertIn("verified complete", source)
        self.assertIn("did not advance", source)
        self.assertIn("Not counting it as complete", source)

    def test_daily_set_still_requires_full_completion_to_mark_the_day(self):
        legacy = _methods("DailySet", "src/dailytasks/runner.py")["_perform_legacy"]
        self.assertIn('totals["final"] == totals["total"]', legacy.replace("'", '"'))

    def test_earn_activities_are_gated_off_the_legacy_dashboard(self):
        method = _methods("DailySet", "src/dailytasks/runner.py")["run_earn_activities"]
        self.assertIn("legacy", method)


if __name__ == "__main__":
    unittest.main()
