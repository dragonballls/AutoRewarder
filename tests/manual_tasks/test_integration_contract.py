"""Static regression contracts for the GUI/API activity-discovery wiring."""

import ast
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _source(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def _api_method(name: str) -> ast.FunctionDef:
    tree = ast.parse(_source("src/api.py"))
    api_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AutoRewarderAPI"
    )
    return next(
        node for node in api_class.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


@unittest.skipUnless(
    all(
        (REPO_ROOT / path).is_file()
        for path in (
            "src/api.py",
            "gui/index.html",
            "gui/script.js",
            "src/dailytasks/runner.py",
            "src/dailytasks/new_dashboard.py",
            "src/search/engine.py",
            "src/emulator/driver.py",
        )
    ),
    "requires the full AutoRewarder repository checkout",
)
class ExistingWindowIntegrationTests(unittest.TestCase):
    def test_test_button_is_in_existing_run_controls(self):
        html = _source("gui/index.html")
        self.assertIn('id="start_btn"', html)
        self.assertIn('id="test_activities_btn"', html)
        self.assertIn('onclick="test_reward_activities()"', html)
        self.assertIn('id="stop_btn"', html)

    def test_gui_calls_existing_pywebview_api(self):
        script = _source("gui/script.js")
        self.assertIn("async function test_reward_activities()", script)
        self.assertIn("pywebview.api.test_activity_discovery()", script)
        self.assertIn("function enable_start_button()", script)
        self.assertIn("test_activities_btn", script)

    def test_test_api_uses_selected_profile_headlessly_and_serializes(self):
        method = ast.unparse(_api_method("test_activity_discovery"))
        self.assertIn("self._run_lock.acquire(blocking=False)", method)
        self.assertIn("self.driver_manager.setup_driver(headless=True, mobile=False)", method)
        self.assertIn("self._review_activity_discovery", method)
        self.assertIn("self._driver.quit()", method)
        self.assertIn("self._run_lock.release()", method)
        self.assertIn("self._balance_lock.locked()", method)

    def test_start_run_resets_discovery_once_per_run(self):
        method = ast.unparse(_api_method("main"))
        self.assertIn("self._activity_discovery_attempted = False", method)

    def test_regular_run_performs_one_discovery_pass(self):
        method = ast.unparse(_api_method("_run_phase"))
        self.assertIn("not self._activity_discovery_attempted", method)
        self.assertIn("_review_activity_discovery", method)
        self.assertIn("source='RUN'", method)
        self.assertIn("self._activity_discovery_attempted = True", method)

    def test_daily_only_run_also_performs_discovery(self):
        method = ast.unparse(_api_method("_run_daily_only"))
        self.assertIn("not self._activity_discovery_attempted", method)
        self.assertIn("_review_activity_discovery", method)
        self.assertIn("source='RUN'", method)
        self.assertIn("self._activity_discovery_attempted = True", method)

    def test_review_helper_only_calls_the_read_only_scanner(self):
        method = ast.unparse(_api_method("_review_activity_discovery"))
        self.assertIn("capture_dashboard_activities(driver)", method)
        self.assertNotIn("driver.get(", method)
        self.assertNotIn("click_element", method)
        self.assertNotIn("send_keys", method)

    def test_existing_activity_handlers_remain_separate(self):
        # New discovery is additive: it does not inject code into the existing
        # search, Daily Set, or Visual Search implementation modules.
        api_method_text = ast.unparse(_api_method("_review_activity_discovery"))
        self.assertIn("capture_dashboard_activities", api_method_text)
        for path in (
            "src/dailytasks/runner.py",
            "src/dailytasks/new_dashboard.py",
            "src/search/engine.py",
            "src/emulator/driver.py",
        ):
            source = _source(path)
            self.assertNotIn("capture_dashboard_activities", source, path)



class CompletionIntegrityTests(unittest.TestCase):
    def test_legacy_daily_set_requires_every_actionable_card_complete(self):
        tree = ast.parse(_source("src/dailytasks/runner.py"))
        daily_set = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "DailySet"
        )
        method = next(
            node for node in daily_set.body
            if isinstance(node, ast.FunctionDef) and node.name == "_perform_legacy"
        )
        source = ast.unparse(method)
        self.assertIn('totals["final"] == totals["total"]', source)
        self.assertNotIn('totals["newly"] > 0', source)
        self.assertIn("stop_event.is_set()", source)

    def test_new_dashboard_compares_card_identity_and_completion(self):
        source = _source("src/dailytasks/new_dashboard.py")
        tree = ast.parse(source)
        helper = next(
            node for node in tree.body
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
            compare(before, [
                {"offerId": "card-a", "isCompleted": True},
                {"offerId": "different-card", "isCompleted": True},
            ])
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
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "StatsManager"
        )
        method = next(
            node for node in stats_class.body
            if isinstance(node, ast.FunctionDef) and node.name == "_merge_defaults"
        )
        source = ast.unparse(method)
        self.assertIn('lifetime["points_estimate"]', source)
        self.assertIn('last_session["points_estimate"]', source)
        self.assertIn("POINTS_PER_CARD", source)
        self.assertNotIn("earn_cards", source)
        self.assertNotIn("quest_tasks", source)

    def test_dashboard_does_not_plot_opened_activities_as_earned_points(self):
        source = _source("gui/dashboard.html")
        self.assertIn("Earn opens", source)
        self.assertIn("Quest opens", source)
        self.assertNotIn("e.earn", source)
        self.assertNotIn("e.quests", source)

    def test_unverified_activity_opens_do_not_inflate_points_estimate(self):
        tree = ast.parse(_source("src/stats/manager.py"))
        stats_class = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "StatsManager"
        )
        method = next(
            node for node in stats_class.body
            if isinstance(node, ast.FunctionDef) and node.name == "record_session"
        )
        assignment = next(
            node for node in ast.walk(method)
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
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "DailySet"
        )
        methods = {
            node.name: ast.unparse(node)
            for node in daily_set.body
            if isinstance(node, ast.FunctionDef)
        }
        self.assertIn("last_daily_set_verified_date", methods["should_perform_daily_set"])
        self.assertNotIn("last_daily_set_date", methods["should_perform_daily_set"])
        self.assertIn("last_daily_set_verified_date", methods["mark_as_completed"])
        self.assertIn('pop("last_daily_set_date"', methods["mark_as_completed"])
        self.assertIn(
            "last_visual_search_verified_date",
            methods["should_perform_visual_search"],
        )
        self.assertNotIn("last_visual_search_date", methods["should_perform_visual_search"])
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
