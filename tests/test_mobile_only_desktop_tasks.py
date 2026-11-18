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
        # Searches live in their own block, guarded so count=0 cannot reach them.
        self.assertIn("def run_searches_block", phase)
        self.assertIn("if self._stop_event.is_set() or not queries_to_search:", phase)
        self.assertIn("queries_to_search = []", phase)

    def test_the_daily_tasks_come_before_the_search_block_by_default(self):
        """
        The order is picked, not fixed: the searches take tens of minutes and a
        failure partway through used to lose the Daily Set with them.
        """
        phase = _method("_run_phase")
        self.assertIn("def run_daily_set_block", phase)
        self.assertIn("def run_searches_block", phase)
        self.assertIn("daily_first = do_daily_set and self.get_daily_first()", phase)
        # The daily-first branch runs the tasks first and the searches second.
        start = phase.index("if daily_first:")
        branch = phase[start:]
        self.assertLess(
            branch.index("run_daily_set_block()"), branch.index("run_searches_block()")
        )

    def test_the_task_only_pass_keeps_every_desktop_task(self):
        """
        A phase that only does tasks still does all three: the Daily Set, the
        earn-page tasks, then the visual search.
        """
        phase = _method("_run_phase")
        block_start = phase.index("def run_daily_set_block")
        block_end = phase.index("daily_first =")
        daily_block = phase[block_start:block_end]
        self.assertIn("self.daily_set.perform_daily_set(", daily_block)
        self.assertIn(
            "self._run_earn_activities(human)",
            daily_block,
            "the earn-page tasks must still run beside the Daily Set",
        )
        # And the visual search still runs once both blocks have had their turn.
        self.assertGreater(
            phase.index("self._run_visual_search_if_needed()"),
            phase.index("if daily_first:"),
            "the visual search runs after the phase's blocks, not inside them",
        )

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
