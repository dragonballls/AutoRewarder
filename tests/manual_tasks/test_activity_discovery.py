import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sys

# Import this isolated module without importing src/__init__.py and the full app.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from manual_tasks.activity_discovery import (
    ActivityKind,
    ActivityStatus,
    capture_dashboard_activities,
    classify_activity,
    load_snapshot,
    main,
    render_report,
    review_activities,
)


class ActivityClassificationTests(unittest.TestCase):
    def test_read_to_earn_is_detected(self):
        item = classify_activity({"title": "Read to Earn", "points": 10})
        self.assertEqual(item.kind, ActivityKind.READ_TO_EARN)
        self.assertEqual(item.points, 10)

    def test_read_articles_to_earn_variant_is_detected(self):
        item = classify_activity({"title": "Read articles to earn points"})
        self.assertEqual(item.kind, ActivityKind.READ_TO_EARN)

    def test_read_stories_variant_is_detected(self):
        item = classify_activity({"title": "Read the latest stories"})
        self.assertEqual(item.kind, ActivityKind.READ_TO_EARN)

    def test_explore_on_bing_is_detected(self):
        item = classify_activity({"title": "Explore on Bing: Space"})
        self.assertEqual(item.kind, ActivityKind.EXPLORE)

    def test_related_topics_description_is_detected(self):
        item = classify_activity({"title": "Continue", "description": "Explore related topics"})
        self.assertEqual(item.kind, ActivityKind.EXPLORE)

    def test_daily_set_is_detected(self):
        item = classify_activity({"title": "Daily Set"})
        self.assertEqual(item.kind, ActivityKind.DAILY_SET)

    def test_punchcard_is_detected_from_url(self):
        item = classify_activity({"title": "Weekly challenge", "url": "https://rewards.bing.com/earn/quest/123"})
        self.assertEqual(item.kind, ActivityKind.PUNCHCARD)

    def test_quiz_and_poll_are_detected(self):
        quiz = classify_activity({"title": "Daily trivia quiz"})
        poll = classify_activity({"title": "Vote in today's poll"})
        self.assertEqual(quiz.kind, ActivityKind.QUIZ)
        self.assertEqual(poll.kind, ActivityKind.POLL)

    def test_more_activities_section_is_detected(self):
        item = classify_activity({"title": "Mystery activity", "section": "More Activities"})
        self.assertEqual(item.kind, ActivityKind.MORE_ACTIVITIES)

    def test_generic_activity_is_other(self):
        item = classify_activity({"title": "Mystery offer"})
        self.assertEqual(item.kind, ActivityKind.OTHER)


class ActivityNormalizationTests(unittest.TestCase):
    def test_progress_and_status_are_derived(self):
        partial = classify_activity({"title": "Quest", "progress": "1/3"})
        done = classify_activity({"title": "Quest", "progress": {"done": 3, "total": 3}})
        self.assertEqual(partial.status, ActivityStatus.IN_PROGRESS)
        self.assertEqual((partial.progress_done, partial.progress_total), (1, 3))
        self.assertEqual(done.status, ActivityStatus.COMPLETED)

    def test_explicit_completion_overrides_progress(self):
        item = classify_activity({"title": "Done", "completed": True, "progress": "0/1"})
        self.assertEqual(item.status, ActivityStatus.COMPLETED)

    def test_only_official_https_links_are_exposed(self):
        trusted = classify_activity({"title": "Read to Earn", "url": "https://www.msn.com/en-us/news"})
        http = classify_activity({"title": "Read to Earn", "url": "http://msn.com/article"})
        external = classify_activity({"title": "Read to Earn", "url": "https://example.com/article"})
        javascript = classify_activity({"title": "Read to Earn", "url": "javascript:alert(1)"})
        self.assertIsNotNone(trusted.manual_url)
        self.assertIsNone(http.manual_url)
        self.assertIsNone(external.manual_url)
        self.assertIsNone(javascript.manual_url)

    def test_url_userinfo_is_rejected(self):
        item = classify_activity({"title": "Offer", "url": "https://user@bing.com/"})
        self.assertIsNone(item.manual_url)

    def test_duplicate_records_are_removed(self):
        records = [
            {"title": "Explore on Bing", "url": "https://bing.com/a"},
            {"title": " explore on bing ", "url": "https://bing.com/a"},
            {"title": "Explore on Bing", "url": "https://bing.com/b"},
        ]
        self.assertEqual(len(review_activities(records)), 2)

    def test_invalid_records_are_rejected(self):
        with self.assertRaises(ValueError):
            classify_activity({"description": "missing title"})
        with self.assertRaises(TypeError):
            review_activities("not a list")

    def test_snapshot_loading_supports_both_shapes(self):
        for payload in (
            [{"title": "Read to Earn"}],
            {"activities": [{"title": "Read to Earn"}]},
        ):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "snapshot.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertEqual(load_snapshot(path)[0].kind, ActivityKind.READ_TO_EARN)

    def test_snapshot_size_limit_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.json"
            path.write_text(" " * 1_000_001, encoding="utf-8")
            with self.assertRaises(ValueError):
                load_snapshot(path)

    def test_report_marks_links_as_manual(self):
        item = classify_activity({"title": "Explore on Bing", "url": "https://bing.com/explore"})
        report = render_report([item])
        self.assertIn("[Open manually]", report)
        self.assertIn("No searches", report)

    def test_report_empty_state(self):
        self.assertIn("No activities", render_report([]))

    def test_non_string_fields_are_normalized(self):
        item = classify_activity({"title": 123, "description": 456, "points": " 12 "})
        self.assertEqual(item.title, "123")
        self.assertEqual(item.description, "456")
        self.assertEqual(item.points, 12)

    def test_invalid_url_and_invalid_progress_are_ignored(self):
        item = classify_activity({
            "title": "Quest",
            "url": "https://[invalid",
            "progress": {"done": 3, "total": 2},
        })
        self.assertIsNone(item.manual_url)
        self.assertIsNone(item.progress_done)
        self.assertIsNone(item.progress_total)

    def test_status_aliases_and_zero_progress(self):
        self.assertEqual(classify_activity({"title": "a", "completed": False}).status, ActivityStatus.AVAILABLE)
        self.assertEqual(classify_activity({"title": "b", "progress": "0/2"}).status, ActivityStatus.AVAILABLE)
        self.assertEqual(classify_activity({"title": "c", "status": "started"}).status, ActivityStatus.IN_PROGRESS)
        self.assertEqual(classify_activity({"title": "d", "status": "mystery"}).status, ActivityStatus.UNKNOWN)

    def test_record_type_validation(self):
        with self.assertRaises(TypeError):
            classify_activity(["not", "an", "object"])

    def test_to_dict_serializes_enums(self):
        item = classify_activity({"title": "Read to Earn", "status": "available"})
        value = item.to_dict()
        self.assertEqual(value["kind"], "read_to_earn")
        self.assertEqual(value["status"], "available")

    def test_snapshot_rejects_invalid_top_level_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(json.dumps({"wrong": []}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_snapshot(path)

    def test_cli_prints_markdown_and_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps([{"title": "Explore on Bing"}]), encoding="utf-8")
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main([str(path)]), 0)
            self.assertIn("Microsoft Rewards activity review", stdout.getvalue())
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main([str(path), "--json"]), 0)
            self.assertIn('"kind": "explore"', stdout.getvalue())

    def test_cli_reports_invalid_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(SystemExit):
                main([str(path)])



class _FakeSwitch:
    def __init__(self, driver):
        self.driver = driver

    def window(self, handle):
        if handle not in self.driver.window_handles:
            raise RuntimeError("unknown window")
        self.driver.current_window_handle = handle


class _FakeDriver:
    def __init__(self, page_records=None, failing_urls=None):
        self.current_url = "https://www.bing.com/search?q=existing"
        self.current_window_handle = "tab-1"
        self.window_handles = ["tab-1"]
        self.page_records = page_records or {}
        self.failing_urls = set(failing_urls or [])
        self.visited = []
        self.switch_to = _FakeSwitch(self)

    def get(self, url):
        self.visited.append(url)
        if url in self.failing_urls:
            raise RuntimeError("simulated navigation failure")
        self.current_url = url

    def execute_script(self, script):
        self.assertion_script = script
        return self.page_records.get(self.current_url, [])


class _RedirectingDriver(_FakeDriver):
    def get(self, url):
        self.visited.append(url)
        if url.startswith("https://rewards.bing.com/"):
            self.current_url = "https://login.live.com/"
        else:
            self.current_url = url


class DashboardCaptureTests(unittest.TestCase):
    def test_capture_reports_login_redirect_instead_of_false_no_activity(self):
        driver = _RedirectingDriver()
        with self.assertRaisesRegex(RuntimeError, "signed-in Edge profile"):
            capture_dashboard_activities(driver)
        self.assertEqual(driver.current_url, "https://www.bing.com/search?q=existing")

    def test_capture_reads_dashboard_pages_and_restores_original_url(self):
        from manual_tasks.activity_discovery import DASHBOARD_URL, EARN_URL

        driver = _FakeDriver({
            DASHBOARD_URL: [{
                "title": "Read to Earn",
                "description": "Read an available MSN story",
                "section": "Dashboard",
                "points": 5,
                "url": "https://www.msn.com/en-us/news",
            }],
            EARN_URL: [{
                "title": "Explore on Bing: Space",
                "description": "Explore related topics",
                "section": "More Activities",
                "points": 10,
                "progress": "0/1",
                "url": "https://www.bing.com/search?q=space",
            }],
        })
        activities = capture_dashboard_activities(driver)
        self.assertEqual(
            {item.kind for item in activities},
            {ActivityKind.READ_TO_EARN, ActivityKind.EXPLORE},
        )
        self.assertEqual(driver.current_url, "https://www.bing.com/search?q=existing")
        self.assertEqual(driver.visited[:2], [DASHBOARD_URL, EARN_URL])
        self.assertEqual(driver.visited[-1], "https://www.bing.com/search?q=existing")
        self.assertNotIn("https://www.msn.com/en-us/news", driver.visited)
        self.assertNotIn("https://www.bing.com/search?q=space", driver.visited)

    def test_capture_returns_partial_results_when_one_dashboard_page_fails(self):
        from manual_tasks.activity_discovery import DASHBOARD_URL, EARN_URL

        driver = _FakeDriver(
            {DASHBOARD_URL: [{"title": "Read to Earn"}]},
            failing_urls={EARN_URL},
        )
        activities = capture_dashboard_activities(driver)
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0].kind, ActivityKind.READ_TO_EARN)
        self.assertEqual(driver.current_url, "https://www.bing.com/search?q=existing")

    def test_capture_raises_when_all_dashboard_pages_fail_and_restores_tab(self):
        from manual_tasks.activity_discovery import DASHBOARD_URL, EARN_URL

        driver = _FakeDriver(failing_urls={DASHBOARD_URL, EARN_URL})
        with self.assertRaises(RuntimeError):
            capture_dashboard_activities(driver)
        self.assertEqual(driver.current_window_handle, "tab-1")
        self.assertEqual(driver.current_url, "https://www.bing.com/search?q=existing")

    def test_capture_rejects_invalid_poll_attempts_without_touching_driver(self):
        driver = _FakeDriver()
        with self.assertRaises(ValueError):
            capture_dashboard_activities(driver, poll_attempts=0)
        self.assertEqual(driver.visited, [])

    def test_scanner_script_contains_no_activity_execution_commands(self):
        from manual_tasks.activity_discovery import _ACTIVITY_SCAN_JS

        for forbidden in ("click()", "send_keys", "window.open", ".submit("):
            self.assertNotIn(forbidden, _ACTIVITY_SCAN_JS)


if __name__ == "__main__":
    unittest.main()
