"""The daily run: when it fires, and what it does first.

Two separate complaints live here, and both were real.

**The time.** Settings says background runs fire "at a random minute after this
time", and the task was registered at the exact minute — so every account set to
09:00 started together, and a fixed start is a fixed pattern.

**The order.** The Daily Set ran after the whole search phase. A full day's
searches take tens of minutes, so a browser or driver failure partway through
lost the fixed daily points as well — the one part of the day worth a streak.
Their own upstream added `--daily-first` for this reason.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.api import _jittered_run_time, _normalize_run_time  # noqa: E402


class RunTimeTests(unittest.TestCase):
    def test_a_time_is_offset_but_stays_in_the_same_hour(self):
        for chosen in ("09:00", "12:30", "00:05", "23:50"):
            actual = _jittered_run_time(chosen, "acc-alice")
            hour, minute = (int(part) for part in actual.split(":"))
            chosen_hour = int(chosen.split(":")[0])
            self.assertEqual(hour, chosen_hour, f"{chosen} -> {actual} changed hour")
            self.assertGreaterEqual(minute, int(chosen.split(":")[1]))
            self.assertLessEqual(minute, 59)

    def test_the_same_account_always_gets_the_same_minute(self):
        """
        A task is re-registered whenever its schedule is edited. If the offset
        moved each time, changing the query count would also move the run time.
        """
        first = _jittered_run_time("09:00", "acc-alice")
        for _ in range(5):
            self.assertEqual(_jittered_run_time("09:00", "acc-alice"), first)

    def test_different_accounts_do_not_all_fire_together(self):
        times = {_jittered_run_time("09:00", f"acc-{i}") for i in range(12)}
        self.assertGreater(
            len(times), 1, "every account fired at the same minute, as before"
        )

    def test_the_end_of_the_day_never_rolls_into_tomorrow(self):
        self.assertEqual(_jittered_run_time("23:59", "acc-alice"), "23:59")

    def test_an_unparseable_time_falls_back_instead_of_raising(self):
        self.assertEqual(_normalize_run_time("nonsense"), _normalize_run_time(None))
        self.assertRegex(_jittered_run_time("nonsense", "a"), r"^\d{2}:\d{2}$")


class OrderTests(unittest.TestCase):
    def _phase(self):
        import ast

        with open(os.path.join(REPO_ROOT, "src", "api.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        klass = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "AutoRewarderAPI"
        )
        method = next(
            node
            for node in klass.body
            if isinstance(node, ast.FunctionDef) and node.name == "_run_phase"
        )
        return ast.unparse(method)

    def test_the_daily_tasks_are_their_own_block(self):
        phase = self._phase()
        self.assertIn("def run_daily_set_block", phase)
        self.assertIn("def run_searches_block", phase)

    def test_daily_first_runs_the_tasks_before_the_searches(self):
        phase = self._phase()
        start = phase.index("if daily_first:")
        branch = phase[start:]
        self.assertLess(
            branch.index("run_daily_set_block()"), branch.index("run_searches_block()")
        )

    def test_turning_it_off_restores_the_old_order(self):
        phase = self._phase()
        start = phase.index("else:", phase.index("if daily_first:"))
        branch = phase[start:]
        self.assertLess(
            branch.index("run_searches_block()"), branch.index("run_daily_set_block()")
        )

    def test_a_phase_that_only_searches_still_checks_the_sign_in(self):
        """
        The mobile phase runs no daily tasks. Without a sign-in check it searched
        an expired session for twenty minutes: every search earned nothing while
        still counting as done, which reads as "mobile search does not work".
        """
        phase = self._phase()
        self.assertIn("_ensure_signed_in", phase)
        # The guard covers a searching phase, not only one that runs tasks.
        self.assertIn(
            "do_daily_set or queries_to_search",
            phase,
            "a search-only phase must still check the session first",
        )


if __name__ == "__main__":
    unittest.main()
