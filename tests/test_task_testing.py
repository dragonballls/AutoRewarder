"""Tests for running one earning task at a time.

The whole point of single-task testing is that it tells you the truth about one
task on your own account. That only holds if:

  * every earning surface is reachable as its own task,
  * a task name is forgiving (label, hyphens, spaces), and
  * a test never marks a day as complete, so using it before a real run cannot
    make that run skip the work.
"""

import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.dailytasks.task_test import (  # noqa: E402
    TASKS,
    TaskTester,
    describe_tasks,
    resolve_task,
    task_names,
)


class TaskCatalogueTests(unittest.TestCase):
    def test_every_earning_surface_is_a_task(self):
        expected = {
            "daily_set",
            "claim",
            "more_activities",
            "quests",
            "read_to_earn",
            "explore",
            "check_in",
            "visual_search",
            "pc_searches",
            "mobile_searches",
        }
        self.assertEqual(set(task_names()), expected)

    def test_every_task_describes_itself(self):
        for spec in describe_tasks():
            self.assertTrue(spec["label"], spec)
            self.assertTrue(spec["description"], spec)
            self.assertIn(spec["page"], ("dashboard", "earn", "browser"))

    def test_task_names_are_unique_and_lowercase(self):
        names = [spec["name"] for spec in describe_tasks()]
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            self.assertEqual(name, name.lower())
            self.assertNotIn(" ", name)


class ResolveTaskTests(unittest.TestCase):
    def test_canonical_names_resolve(self):
        for name in task_names():
            self.assertEqual(resolve_task(name), name)

    def test_labels_resolve(self):
        self.assertEqual(resolve_task("Read to Earn"), "read_to_earn")
        self.assertEqual(resolve_task("Daily check-in"), "check_in")
        self.assertEqual(resolve_task("Explore on Bing"), "explore")
        self.assertEqual(resolve_task("Visual Search"), "visual_search")

    def test_loose_spellings_resolve(self):
        self.assertEqual(resolve_task("read-to-earn"), "read_to_earn")
        self.assertEqual(resolve_task("Read To Earn"), "read_to_earn")
        self.assertEqual(resolve_task("  read_to_earn  "), "read_to_earn")
        self.assertEqual(resolve_task("DAILY SET"), "daily_set")

    def test_unknown_names_are_rejected_with_the_valid_ones(self):
        with self.assertRaises(ValueError) as caught:
            resolve_task("do my taxes")
        self.assertIn("daily_set", str(caught.exception))

    def test_empty_names_are_rejected(self):
        with self.assertRaises(ValueError):
            resolve_task("")
        with self.assertRaises(ValueError):
            resolve_task(None)


class _FakeDailySet:
    """Records which status markers a test run tries to write."""

    dashboard_variant = "new"

    def __init__(self):
        self.logger = None
        self._new_handler = None
        self.last_totals = {"already": 1, "newly": 2, "final": 3, "total": 3}
        self.marked = 0

    def perform_daily_set(self, driver, human, stop_event=None, variant=None):
        return True

    def mark_as_completed(self):
        self.marked += 1


class _FakeHandler:
    def __init__(self):
        self.last_totals = {"earn": 0, "quests": 0, "attempted": 0}
        self.claimed = 0

    def _run_claim(self, driver, human, stop_event=None):
        self.claimed += 1
        self.last_totals["attempted"] += 1

    def _run_more_activities(self, driver, human, stop_event=None):
        self.last_totals["earn"] += 2

    def _run_quests(self, driver, human, stop_event=None):
        self.last_totals["quests"] += 1


class _FakeEarn:
    def __init__(self, verified):
        self.verified = verified

    def _open_earn_page(self, driver):
        return True

    def run_read_to_earn(self, driver, human, stop_event=None):
        return self.verified

    def run_explore(self, driver, human, stop_event=None):
        return self.verified

    def run_check_in(self, driver, human, stop_event=None):
        return self.verified


class SingleTaskRunTests(unittest.TestCase):
    def test_daily_set_test_reports_cards_and_marks_nothing(self):
        daily = _FakeDailySet()
        result = TaskTester().run("daily_set", object(), object(), daily_set=daily)

        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertIn("3/3", result["detail"])
        # A test must never write the day's completion marker.
        self.assertEqual(daily.marked, 0)

    def test_read_to_earn_reports_verified_only_when_the_card_advanced(self):
        result = TaskTester().run(
            "read_to_earn", object(), object(), earn=_FakeEarn(verified=1)
        )
        self.assertTrue(result["verified"])

        result = TaskTester().run(
            "read_to_earn", object(), object(), earn=_FakeEarn(verified=0)
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["verified"], "a non-advancing card must not verify")
        self.assertTrue(result["notes"])

    def test_claim_reports_it_cannot_be_verified(self):
        daily = _FakeDailySet()
        daily._new_handler = _FakeHandler()
        result = TaskTester().run("claim", object(), object(), daily_set=daily)

        self.assertTrue(result["ok"])
        self.assertFalse(result["verified"], "Rewards cannot confirm a claim here")
        self.assertTrue(any("balance" in note for note in result["notes"]))

    def test_more_activities_reports_opens_without_claiming_credit(self):
        daily = _FakeDailySet()
        daily._new_handler = _FakeHandler()
        result = TaskTester().run(
            "more_activities", object(), object(), daily_set=daily
        )

        self.assertTrue(result["ok"])
        self.assertFalse(result["verified"])
        self.assertIn("2", result["detail"])

    def test_an_unknown_task_is_reported_not_raised(self):
        result = TaskTester().run("nonsense", object(), object())
        self.assertFalse(result["ok"])
        self.assertIn("Unknown task", result["detail"])

    def test_a_failing_task_is_caught_and_described(self):
        class Exploding:
            def perform_daily_set(self, *args, **kwargs):
                raise RuntimeError("browser died")

        result = TaskTester().run(
            "daily_set", object(), object(), daily_set=Exploding()
        )
        self.assertFalse(result["ok"])
        self.assertIn("browser died", result["detail"])

    def test_a_dashboard_task_without_a_browser_is_refused(self):
        result = TaskTester().run("daily_set", None, None)
        self.assertFalse(result["ok"])
        self.assertIn("No browser", result["detail"])

    def test_every_task_has_a_runner(self):
        """A task in the catalogue with no implementation is a dead entry."""
        tester = TaskTester()
        for name in task_names():
            if TASKS[name]["page"] == "browser":
                continue
            result = tester.run(
                name,
                driver=object(),
                human=object(),
                daily_set=_FakeDailySet(),
                earn=_FakeEarn(1),
            )
            # It may report "not verified", but never "no runner".
            self.assertNotIn("No runner", result["detail"], name)


class NoDayMarkerTests(unittest.TestCase):
    def test_the_tester_never_calls_a_marking_method(self):
        """Guard the rule in the source, so a future runner can't quietly mark."""
        with open(
            os.path.join(REPO_ROOT, "src/dailytasks/task_test.py"), encoding="utf-8"
        ) as handle:
            source = handle.read()
        for marker in ("mark_as_completed", "mark_visual_search_as_completed"):
            self.assertNotIn(marker, source, f"{marker} must not be called by a test")

    def test_the_api_test_path_does_not_mark_either(self):
        with open(os.path.join(REPO_ROOT, "src/api.py"), encoding="utf-8") as handle:
            source = handle.read()
        start = source.index("def test_task(")
        end = source.index("def _run_earn_activities(", start)
        body = source[start:end]
        self.assertNotIn("mark_as_completed", body)


class VisSearchResultTests(unittest.TestCase):
    def test_visual_search_reports_the_api_answer(self):
        api = mock.Mock()
        api._run_visual_search_if_needed.return_value = True
        result = TaskTester().run("visual_search", None, None, api=api)
        self.assertTrue(result["verified"])

        api._run_visual_search_if_needed.return_value = False
        result = TaskTester().run("visual_search", None, None, api=api)
        self.assertTrue(result["ok"])
        self.assertFalse(result["verified"])


if __name__ == "__main__":
    unittest.main()
