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


if __name__ == "__main__":
    unittest.main()
