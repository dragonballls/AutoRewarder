"""Completion-integrity contracts for the points-earning paths.

These assert that a task is only recorded as complete when Rewards confirms it,
and that nothing which merely opened an activity is counted as earned points.
They used to live beside the activity-discovery contracts; only the parts that
still describe shipped behaviour are kept here.
"""

import ast
from pathlib import Path
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def _api_method(name: str) -> ast.FunctionDef:
    tree = ast.parse(_source("src/api.py"))
    api_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AutoRewarderAPI"
    )
    return next(
        node
        for node in api_class.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


class CompletionIntegrityTests(unittest.TestCase):
    def test_legacy_daily_set_requires_every_actionable_card_complete(self):
        tree = ast.parse(_source("src/dailytasks/runner.py"))
        daily_set = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "DailySet"
        )
        method = next(
            node
            for node in daily_set.body
            if isinstance(node, ast.FunctionDef) and node.name == "_perform_legacy"
        )
        source = ast.unparse(method).replace("'", '"')
        self.assertIn('totals["final"] == totals["total"]', source)
        self.assertNotIn('totals["newly"] > 0', source)
        self.assertIn("stop_event.is_set()", source)

    def test_new_dashboard_compares_card_identity_and_completion(self):
        source = _source("src/dailytasks/new_dashboard.py")
        tree = ast.parse(source)
        helper = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_verified_daily_set_counts"
        )
        namespace = {}
        module = ast.Module(body=[helper], type_ignores=[])
        exec(compile(module, "<completion-helper-test>", "exec"), namespace)
        compare = namespace["_verified_daily_set_counts"]

        before = [
            {"offerId": "card-a", "isCompleted": False},
            {"offerId": "card-b", "isCompleted": False},
        ]
        partial_after = [
            {"offerId": "card-a", "isCompleted": True},
            {"offerId": "card-b", "isCompleted": False},
        ]
        complete_after = [
            {"offerId": "card-a", "isCompleted": True},
            {"offerId": "card-b", "isCompleted": True},
        ]
        self.assertEqual(
            compare(before, partial_after),
            {"total": 2, "final": 1, "newly": 1},
        )
        self.assertEqual(
            compare(before, complete_after),
            {"total": 2, "final": 2, "newly": 2},
        )
        self.assertIsNone(compare(before, complete_after[:1]))
        self.assertIsNone(
            compare(
                before,
                [
                    {"offerId": "card-a", "isCompleted": True},
                    {"offerId": "different-card", "isCompleted": True},
                ],
            )
        )

    def test_new_dashboard_marks_day_only_when_all_original_cards_are_complete(self):
        source = _source("src/dailytasks/new_dashboard.py")
        self.assertIn("_verified_daily_set_counts(todays, after)", source)
        self.assertIn(
            'if verified and self.last_totals["final"] == self.last_totals["total"]:',
            source,
        )
        self.assertNotIn("if newly > 0:", source)

    def test_visual_search_requires_a_positive_progress_delta(self):
        check = ast.unparse(_api_method("_check_visual_search_credited"))
        run = ast.unparse(_api_method("_run_visual_search_if_needed"))
        self.assertIn("before is None", check)
        self.assertIn("done > before_done", check)
        self.assertIn("read_visual_search_progress(self._driver)", run)
        self.assertIn("elif credited is None", run)

    def test_saved_points_estimates_are_migrated_away_from_unverified_opens(self):
        tree = ast.parse(_source("src/stats/manager.py"))
        stats_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "StatsManager"
        )
        method = next(
            node
            for node in stats_class.body
            if isinstance(node, ast.FunctionDef) and node.name == "_merge_defaults"
        )
        source = ast.unparse(method).replace("'", '"')
        self.assertIn('lifetime["points_estimate"]', source)
        self.assertIn('last_session["points_estimate"]', source)
        self.assertIn("POINTS_PER_CARD", source)
        self.assertNotIn("earn_cards", source)
        self.assertNotIn("quest_tasks", source)

    def test_dashboard_does_not_plot_opened_activities_as_earned_points(self):
        source = _source("gui/dashboard.html")
        self.assertIn("Earn opens", source)
        self.assertIn("Quest opens", source)
        self.assertNotIn("earn: (v.earn", source)
        self.assertNotIn("quests: (v.quests", source)

    def test_unverified_activity_opens_do_not_inflate_points_estimate(self):
        tree = ast.parse(_source("src/stats/manager.py"))
        stats_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "StatsManager"
        )
        method = next(
            node
            for node in stats_class.body
            if isinstance(node, ast.FunctionDef) and node.name == "record_session"
        )
        assignment = next(
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "session_estimate"
                for target in node.targets
            )
        )
        expression = ast.unparse(assignment.value)
        self.assertIn("cards * POINTS_PER_CARD", expression)
        self.assertNotIn("earn", expression)
        self.assertNotIn("quests", expression)

    def test_legacy_unverified_status_dates_are_ignored(self):
        tree = ast.parse(_source("src/dailytasks/runner.py"))
        daily_set = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "DailySet"
        )
        methods = {
            node.name: ast.unparse(node).replace("'", '"')
            for node in daily_set.body
            if isinstance(node, ast.FunctionDef)
        }
        self.assertIn(
            "last_daily_set_verified_date", methods["should_perform_daily_set"]
        )
        self.assertNotIn("last_daily_set_date", methods["should_perform_daily_set"])
        self.assertIn("last_daily_set_verified_date", methods["mark_as_completed"])
        self.assertIn('pop("last_daily_set_date"', methods["mark_as_completed"])
        self.assertIn(
            "last_visual_search_verified_date",
            methods["should_perform_visual_search"],
        )
        self.assertNotIn(
            "last_visual_search_date", methods["should_perform_visual_search"]
        )
        self.assertIn(
            "last_visual_search_verified_date",
            methods["mark_visual_search_as_completed"],
        )
        self.assertIn(
            'pop("last_visual_search_date"',
            methods["mark_visual_search_as_completed"],
        )

    def test_fake_sequential_demo_has_been_removed_everywhere(self):
        for path in (
            "gui/script.js",
            "gui/index.html",
            "src/api.py",
            "src/accounts/settings.py",
            "USER_GUIDE.md",
        ):
            source = _source(path)
            self.assertNotRegex(source, r"demoSequence|demo_sequence")
        self.assertIn("Earn opens", _source("gui/dashboard.html"))
        self.assertIn("Quest opens", _source("gui/dashboard.html"))


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
