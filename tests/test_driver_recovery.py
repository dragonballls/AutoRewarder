"""Tests for Edge launch recovery.

The failure these cover is the one that stopped real runs on Windows:

    session not created: Microsoft Edge failed to start: crashed.
    (DevToolsActivePort file doesn't exist)

That message is Chromium reporting a symptom. The causes are fixable — a profile
folder that doesn't exist yet, abandoned Chromium lock files, or Edge processes
still holding the profile — so a launch must clear them and retry before it
gives up, and it must then say something a person can act on.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from selenium.common.exceptions import SessionNotCreatedException  # noqa: E402

from src.emulator import driver as driver_module  # noqa: E402
from src.emulator.driver import DriverManager, describe_start_failure  # noqa: E402

# The exact message from the Windows failure report.
DEVTOOLS_ERROR = (
    "Message: session not created: Microsoft Edge failed to start: crashed.\n"
    "(session not created: DevToolsActivePort file doesn't exist)\n"
    "(The process started from msedge location "
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe is no "
    "longer running, so msedgedriver is assuming that msedge has crashed.)"
)


class DescribeFailureTests(unittest.TestCase):
    def test_devtools_error_names_the_real_causes_and_the_way_out(self):
        text = describe_start_failure(SessionNotCreatedException(DEVTOOLS_ERROR))

        self.assertIn("profile", text.casefold())
        # The actionable steps, not just a diagnosis.
        self.assertIn("Close every Microsoft Edge window", text)
        self.assertIn("Startup boost", text)

    def test_a_locked_profile_is_named(self):
        text = describe_start_failure(
            SessionNotCreatedException("The user data directory is already in use")
        )
        self.assertIn("another Edge window", text)

    def test_a_missing_driver_is_named(self):
        text = describe_start_failure(
            SessionNotCreatedException("cannot find msedgedriver")
        )
        self.assertIn("driver is missing", text)

    def test_an_unknown_failure_is_not_invented(self):
        self.assertEqual(describe_start_failure(RuntimeError("something else")), "")


class ProfilePreparationTests(unittest.TestCase):
    def test_a_missing_profile_folder_is_created(self):
        with tempfile.TemporaryDirectory() as base:
            profile = os.path.join(base, "deep", "not", "there")
            manager = DriverManager(profile_path=profile)
            manager._prepare_profile_dir()
            self.assertTrue(os.path.isdir(profile))

    def test_an_unwritable_parent_is_reported_clearly(self):
        with tempfile.TemporaryDirectory() as base:
            blocker = os.path.join(base, "blocker")
            with open(blocker, "w", encoding="utf-8") as handle:
                handle.write("not a directory")
            manager = DriverManager(profile_path=os.path.join(blocker, "profile"))

            with self.assertRaises(RuntimeError) as caught:
                manager._prepare_profile_dir()

            self.assertIn("could not be created", str(caught.exception))


class SingletonLockTests(unittest.TestCase):
    def test_abandoned_locks_are_removed(self):
        with tempfile.TemporaryDirectory() as profile:
            for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                with open(os.path.join(profile, name), "w", encoding="utf-8"):
                    pass

            manager = DriverManager(profile_path=profile)
            self.assertTrue(manager._remove_singleton_locks())
            self.assertEqual(
                [n for n in os.listdir(profile) if n.startswith("Singleton")], []
            )

    def test_no_locks_is_success(self):
        with tempfile.TemporaryDirectory() as profile:
            self.assertTrue(
                DriverManager(profile_path=profile)._remove_singleton_locks()
            )


class _FakeOptions:
    """Records the arguments added, so the flags can be asserted."""

    def __init__(self):
        self.args = []
        self.experimental = {}

    def add_argument(self, value):
        self.args.append(value)

    def add_experimental_option(self, name, value):
        self.experimental[name] = value


class LaunchRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile = os.path.join(self.tmp.name, "profile")
        self.manager = DriverManager(profile_path=self.profile)

        patcher = mock.patch.object(driver_module, "Options", _FakeOptions)
        patcher.start()
        self.addCleanup(patcher.stop)

        sleeper = mock.patch.object(driver_module.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_a_transient_failure_is_retried_and_succeeds(self):
        """One lock-clear, then success on the next attempt — not an error."""
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise SessionNotCreatedException(DEVTOOLS_ERROR)
            return "driver"

        with mock.patch.object(driver_module.webdriver, "Edge", side_effect=flaky):
            result = self.manager.setup_driver(headless=True)

        self.assertEqual(result, "driver")
        self.assertEqual(calls["n"], 3, "it should have retried")

    def test_recovery_runs_before_each_attempt(self):
        with mock.patch.object(
            driver_module.webdriver,
            "Edge",
            side_effect=SessionNotCreatedException(DEVTOOLS_ERROR),
        ), mock.patch.object(
            self.manager, "_remove_singleton_locks", return_value=True
        ) as locks, mock.patch.object(
            self.manager, "close_running_edge"
        ) as kill:
            with self.assertRaises(RuntimeError):
                self.manager.setup_driver(headless=True)

        self.assertEqual(locks.call_count, self.manager.START_ATTEMPTS)
        self.assertTrue(kill.called, "a held profile must be released between attempts")

    def test_failure_is_reported_with_an_explanation_not_a_backtrace(self):
        with mock.patch.object(
            driver_module.webdriver,
            "Edge",
            side_effect=SessionNotCreatedException(DEVTOOLS_ERROR),
        ):
            with self.assertRaises(RuntimeError) as caught:
                self.manager.setup_driver(headless=True)

        message = str(caught.exception)
        self.assertIn("could not be started", message)
        self.assertIn("Startup boost", message)
        # The raw Chromium dump must not be the whole story.
        self.assertNotIn("[0x7ff7", message)

    def test_a_missing_profile_is_created_before_launching(self):
        with mock.patch.object(driver_module.webdriver, "Edge", return_value="driver"):
            self.manager.setup_driver(headless=True)
        self.assertTrue(os.path.isdir(self.profile))

    def test_no_account_still_raises_a_clear_error(self):
        manager = DriverManager(profile_path=None)
        with self.assertRaises(RuntimeError) as caught:
            manager.setup_driver()
        self.assertIn("No account selected", str(caught.exception))

    def test_the_launch_disables_edge_background_services(self):
        """Startup boost and background networking are what keep the lock held."""
        options = self.manager._build_options(
            headless=False, disable_identity=False, mobile=False
        )
        joined = " ".join(options.args)
        self.assertIn("--disable-background-networking", joined)
        self.assertIn("--disable-component-update", joined)

    def test_mobile_still_sets_its_user_agent(self):
        options = self.manager._build_options(
            headless=False, disable_identity=False, mobile=True
        )
        joined = " ".join(options.args)
        self.assertIn("iPhone", joined)
        self.assertIn("412,915", joined)


class StopClearsDriverTests(unittest.TestCase):
    def test_stop_quits_once_and_clears_the_handle(self):
        """
        The old stop() left the dead driver on the instance, so the run thread
        could quit it a second time and the next launch raced a closing Edge.
        """
        import src.api as api_module

        api = api_module.AutoRewarderAPI.__new__(api_module.AutoRewarderAPI)
        api.log = lambda message: None
        api._stop_event = mock.Mock()
        api._run_lock = mock.Mock()
        api._run_lock.locked.return_value = True

        driver = mock.Mock()
        api._driver = driver

        self.assertTrue(api.stop())
        driver.quit.assert_called_once()
        self.assertIsNone(api._driver, "the handle must be cleared, not left dead")

    def test_stop_twice_does_not_quit_a_dead_driver(self):
        import src.api as api_module

        api = api_module.AutoRewarderAPI.__new__(api_module.AutoRewarderAPI)
        api.log = lambda message: None
        api._stop_event = mock.Mock()
        api._run_lock = mock.Mock()
        api._run_lock.locked.return_value = True

        driver = mock.Mock()
        api._driver = driver

        api.stop()
        api.stop()

        self.assertEqual(driver.quit.call_count, 1)


if __name__ == "__main__":
    unittest.main()
