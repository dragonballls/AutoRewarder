"""A Mobile-only run must still earn the desktop-only Rewards points.

Daily Set, Read to Earn, Explore on Bing, the daily check-in and Visual Search
are desktop surfaces: they are driven by the PC phase. When a user turns PC
searches off and keeps Mobile searches, that phase never ran, so every one of
those tasks was silently skipped and the day's task points were lost while the
run still reported "Done!".

These tests read the orchestration code, so removing the task-only pass fails
here instead of quietly costing points.
"""

import ast
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _source(relative_path):
    with open(os.path.join(REPO_ROOT, relative_path), encoding="utf-8") as handle:
        return handle.read()


def _method(name):
    tree = ast.parse(_source("src/api.py"))
    api_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AutoRewarderAPI"
    )
    method = next(
        node
        for node in api_class.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    return ast.unparse(method)


def _positions(haystack, needles):
    """Offsets of each needle, failing loudly when one is missing."""
    found = []
    for needle in needles:
        if needle not in haystack:
            raise AssertionError(f"missing from the orchestration: {needle}")
        found.append(haystack.index(needle))
    return found


class MobileOnlyDesktopTaskPassTests(unittest.TestCase):
    def test_standard_run_earns_desktop_tasks_without_pc_searches(self):
        main = _method("main")
        self.assertIn("self._run_phase(mobile=False, count=0, do_daily_set=True", main)

    def test_standard_run_tasks_come_before_mobile_searches(self):
        main = _method("main")
        desktop_pass = main.index(
            "self._run_phase(mobile=False, count=0, do_daily_set=True"
        )
        mobile_search = main.index("self._run_phase(mobile=True, count=mobile_count")
        self.assertLess(desktop_pass, mobile_search)

    def test_advanced_run_earns_desktop_tasks_without_pc_searches(self):
        advanced = _method("_run_advanced_schedule")
        self.assertIn(
            "self._run_phase(mobile=False, count=0, do_daily_set=True", advanced
        )

    def test_advanced_run_tasks_come_before_the_batch_loop(self):
        advanced = _method("_run_advanced_schedule")
        desktop_pass = advanced.index(
            "self._run_phase(mobile=False, count=0, do_daily_set=True"
        )
        batch_loop = advanced.index("for i in range(num_batches)")
        self.assertLess(desktop_pass, batch_loop)

    def test_the_task_only_pass_performs_no_searches(self):
        phase = _method("_run_phase")
        # Searches are guarded, so count=0 cannot reach perform_searches.
        self.assertIn("if queries_to_search:", phase)
        self.assertIn("queries_to_search = []", phase)

    def test_the_task_only_pass_keeps_every_desktop_task(self):
        phase = _method("_run_phase")
        ordered = _positions(
            phase,
            [
                "self.daily_set.perform_daily_set(",
                "self._run_earn_activities(human)",
                "self._run_visual_search_if_needed()",
            ],
        )
        self.assertEqual(ordered, sorted(ordered))

    def test_the_earn_pass_still_covers_read_explore_and_check_in(self):
        # The three earn-page tasks are owned by the runner, reached through
        # _run_earn_activities; the desktop task pass must keep calling it.
        phase = _method("_run_phase")
        self.assertIn("self._run_earn_activities(human)", phase)

        earn = _method("_run_earn_activities")
        self.assertIn("self.daily_set.run_earn_activities(", earn)
        for task in ("read", "explore", "check_in"):
            self.assertIn(task, earn)

        runner = _source("src/dailytasks/runner.py")
        self.assertIn("activities.run(driver, human, stop_event=stop_event)", runner)

    def test_zero_count_phase_is_honest_about_doing_no_searches(self):
        phase = _method("_run_phase")
        self.assertIn("dashboard tasks only, no searches", phase)

    def test_no_pc_searches_still_builds_no_queries(self):
        phase = _method("_run_phase")
        # A zero-count pass must not call the LLM/query builder at all.
        guarded = phase.index("if count > 0:")
        builder = phase.index("self._build_queries(count)")
        self.assertLess(guarded, builder)


if __name__ == "__main__":
    unittest.main()
