"""The update button: what it offers, what it refuses, and how it ends.

The person asked for one thing — press update, get the new version, no
download page and no installer. These tests hold the API to it, and to the
cases where doing nothing is the right answer: a run in progress, a source
checkout, a download already under way.
"""

import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import src.api as api_module  # noqa: E402

RELEASE = {
    "tag_name": "v99.0",
    "assets": [
        {
            "name": "AutoRewarder-windows.zip",
            "browser_download_url": "https://example.test/win.zip",
            "size": 2048,
        },
        {
            "name": "AutoRewarder-linux.tar.gz",
            "browser_download_url": "https://example.test/linux.tar.gz",
            "size": 4096,
        },
    ],
}


def _api():
    api = api_module.AutoRewarderAPI.__new__(api_module.AutoRewarderAPI)
    api.logs = []
    api.log = api.logs.append
    api._update_lock = mock.Mock()
    api._update_lock.locked.return_value = False
    api._update_cancel = mock.Mock()
    api._update_cancel.is_set.return_value = False
    api._update_progress = {"phase": "idle", "done": 0, "total": None}
    api._update_notified_at = 0.0
    api._update_notified_phase = "idle"
    api._run_lock = mock.Mock()
    api._run_lock.locked.return_value = False
    api.is_driver_loading = False
    api._webview_window = None
    return api


class CheckTests(unittest.TestCase):
    def test_an_available_update_offers_the_download_for_this_machine(self):
        api = _api()
        with mock.patch.object(
            api_module.updater, "latest_release", return_value=RELEASE
        ), mock.patch.object(api_module.updater, "is_frozen", return_value=True):
            result = api.check_updates_now()

        self.assertTrue(result["ok"])
        self.assertTrue(result["update_available"])
        self.assertEqual(result["latest"], "v99.0")
        self.assertTrue(result["can_update_in_place"])
        self.assertTrue(result["asset"]["name"].startswith("AutoRewarder-"))

    def test_being_up_to_date_offers_nothing(self):
        api = _api()
        # describe_plan compares against src.config.CURRENT_VERSION, so that is
        # what has to say "we are already on it".
        with mock.patch.object(
            api_module.updater, "latest_release", return_value=RELEASE
        ), mock.patch.object(
            api_module.updater, "is_frozen", return_value=True
        ), mock.patch(
            "src.config.CURRENT_VERSION", "v99.0"
        ):
            result = api.check_updates_now()

        self.assertTrue(result["ok"])
        self.assertFalse(result["update_available"])

    def test_an_offline_check_is_not_an_error_to_the_ui(self):
        api = _api()
        with mock.patch.object(api_module.updater, "latest_release", return_value=None):
            result = api.check_updates_now()

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unreachable")
        self.assertIsNone(result["latest"])

    def test_a_source_run_is_not_offered_in_place_updating(self):
        """A source checkout has no files to swap, so the button must not try."""
        api = _api()
        with mock.patch.object(
            api_module.updater, "latest_release", return_value=RELEASE
        ), mock.patch.object(api_module.updater, "is_frozen", return_value=False):
            result = api.check_updates_now()

        self.assertTrue(result["update_available"])
        self.assertFalse(result["can_update_in_place"])


class InstallRefusalTests(unittest.TestCase):
    def test_a_running_job_blocks_the_update(self):
        """Restarting mid-run would lose the day's points."""
        api = _api()
        api._run_lock.locked.return_value = True
        result = api.install_update()
        self.assertFalse(result["ok"])
        self.assertIn("run is in progress", result["error"])

    def test_a_source_run_refuses_and_says_why(self):
        api = _api()
        with mock.patch.object(api_module.updater, "is_frozen", return_value=False):
            result = api.install_update()
        self.assertFalse(result["ok"])
        self.assertIn("source", result["error"])

    def test_a_second_update_is_refused(self):
        api = _api()
        api._update_lock.locked.return_value = True
        with mock.patch.object(api_module.updater, "is_frozen", return_value=True):
            result = api.install_update()
        self.assertFalse(result["ok"])
        self.assertIn("already running", result["error"])

    def test_the_lock_is_taken_before_the_worker_starts(self):
        """
        The worker used to take the lock itself, so two clicks in the same
        instant both passed the "is one running?" check and started two
        downloads of the same 300 MB build.
        """
        api = _api()
        api._update_lock = mock.Mock()
        api._update_lock.locked.return_value = False
        api._update_lock.acquire.return_value = False  # a second click lost the race

        with mock.patch.object(
            api_module.updater, "is_frozen", return_value=True
        ), mock.patch.object(
            api_module.updater, "install_dir", return_value="/apps/AutoRewarder"
        ), mock.patch.object(
            api_module.threading, "Thread"
        ) as thread:
            result = api.install_update()

        self.assertFalse(result["ok"])
        self.assertIn("already running", result["error"])
        self.assertFalse(thread.called, "a second download was started")

    def test_cancelling_is_a_request_not_a_fact(self):
        api = _api()
        self.assertTrue(api.cancel_update())
        api._update_cancel.set.assert_called_once()


class WorkerTests(unittest.TestCase):
    def _run(self, api, download_result, unpack_result, apply_result):
        with mock.patch.object(
            api_module.updater, "latest_release", return_value=RELEASE
        ), mock.patch.object(
            api_module.updater, "describe_plan"
        ) as plan, mock.patch.object(
            api_module.updater, "download", return_value=download_result
        ), mock.patch.object(
            api_module.updater, "unpack", return_value=unpack_result
        ), mock.patch.object(
            api_module.updater, "apply_and_restart", return_value=apply_result
        ), mock.patch.object(
            api_module.updater, "staging_dir", return_value="/tmp/stage"
        ), mock.patch.object(
            api_module.updater, "clean_staging"
        ), mock.patch.object(
            api_module.time, "sleep"
        ), mock.patch.object(
            api, "_quit_for_update"
        ) as quit_update:
            plan.return_value = {
                "available": True,
                "latest": "v99.0",
                "asset": {
                    "name": "AutoRewarder-linux.tar.gz",
                    "url": "https://example.test/linux.tar.gz",
                    "size": 4096,
                },
                "size": 4096,
                "reason": "",
            }
            api._update_lock = mock.Mock()
            api._install_update_worker("/apps/AutoRewarder")
        return quit_update

    def test_a_good_update_restarts_into_it(self):
        api = _api()
        quit_update = self._run(
            api, ("/tmp/f", ""), ("/tmp/stage/AutoRewarder", ""), (True, "")
        )
        quit_update.assert_called_once()
        self.assertEqual(api._update_progress["phase"], "restarting")

    def test_a_failed_download_changes_nothing(self):
        api = _api()
        quit_update = self._run(api, (None, "GitHub answered 502"), None, None)
        self.assertFalse(quit_update.called)
        self.assertEqual(api._update_progress["phase"], "failed")
        self.assertIn("502", api._update_progress["detail"])

    def test_a_build_that_will_not_unpack_changes_nothing(self):
        api = _api()
        quit_update = self._run(
            api, ("/tmp/f", ""), (None, "no AutoRewarder in it"), None
        )
        self.assertFalse(quit_update.called)
        self.assertEqual(api._update_progress["phase"], "failed")

    def test_a_helper_that_will_not_start_changes_nothing(self):
        api = _api()
        quit_update = self._run(
            api,
            ("/tmp/f", ""),
            ("/tmp/stage/AutoRewarder", ""),
            (False, "could not start"),
        )
        self.assertFalse(quit_update.called)
        self.assertEqual(api._update_progress["phase"], "failed")

    def test_a_cancel_after_unpacking_leaves_the_install_alone(self):
        api = _api()
        api._update_cancel.is_set.return_value = True
        quit_update = self._run(
            api, ("/tmp/f", ""), ("/tmp/stage/AutoRewarder", ""), (True, "")
        )
        self.assertFalse(quit_update.called)
        self.assertEqual(api._update_progress["phase"], "cancelled")

    def test_the_unpacked_folder_is_what_the_helper_is_given(self):
        """
        `unpack` strips the archive's top-level folder, so the executable is in
        the folder it *returns* — not in the staging root it was given. Handing
        over the root fails the helper's "is the exe there" check on every real
        update, and a test that staged the folder by hand could never see it.
        """
        api = _api()
        staged_root = "/tmp/stage-root"
        unpacked = "/tmp/stage-root/AutoRewarder"

        with mock.patch.object(
            api_module.updater, "latest_release", return_value=RELEASE
        ), mock.patch.object(
            api_module.updater,
            "describe_plan",
            return_value={
                "available": True,
                "latest": "v99.0",
                "asset": {"name": "AutoRewarder-linux.tar.gz", "url": "u", "size": 1},
                "size": 1,
            },
        ), mock.patch.object(
            api_module.updater, "download", return_value=("/tmp/f", "")
        ), mock.patch.object(
            api_module.updater, "staging_dir", return_value=staged_root
        ), mock.patch.object(
            api_module.updater, "unpack", return_value=(unpacked, "")
        ), mock.patch.object(
            api_module.updater, "clean_staging"
        ), mock.patch.object(
            api_module.updater, "apply_and_restart", return_value=(True, "")
        ) as apply, mock.patch.object(
            api_module.time, "sleep"
        ), mock.patch.object(
            api, "_quit_for_update"
        ):
            api._update_lock = mock.Mock()
            api._install_update_worker("/apps/AutoRewarder")

        self.assertEqual(
            apply.call_args[0][0],
            unpacked,
            "the helper must be given the folder holding the executable",
        )

    def test_the_download_is_removed_once_it_is_unpacked(self):
        """A build is ~300 MB; keeping it costs that much disk every update."""
        api = _api()
        removed = []
        with mock.patch.object(
            api_module.updater, "latest_release", return_value=RELEASE
        ), mock.patch.object(
            api_module.updater,
            "describe_plan",
            return_value={
                "available": True,
                "latest": "v99.0",
                "asset": {"name": "AutoRewarder-linux.tar.gz", "url": "u", "size": 1},
                "size": 1,
            },
        ), mock.patch.object(
            api_module.updater, "download", return_value=("/tmp/f", "")
        ), mock.patch.object(
            api_module.updater, "staging_dir", return_value="/tmp/stage"
        ), mock.patch.object(
            api_module.updater, "unpack", return_value=(None, "bad archive")
        ), mock.patch.object(
            api_module.updater, "clean_staging"
        ), mock.patch.object(
            api_module.shutil, "rmtree", side_effect=lambda p, **k: removed.append(p)
        ), mock.patch.object(
            api_module.time, "sleep"
        ), mock.patch.object(
            api, "_quit_for_update"
        ):
            api._update_lock = mock.Mock()
            api._install_update_worker("/apps/AutoRewarder")

        self.assertTrue(removed, "the download folder was never cleaned up")
        self.assertIn("autorewarder-download-", removed[0])


class QuitTests(unittest.TestCase):
    def test_the_update_quit_bypasses_close_to_tray(self):
        """
        Closing to the tray leaves the process alive, and the swap script waits
        for it to exit — so the update would never land. This path must really
        end the app.
        """
        api = _api()
        api._exiting_for_update = False
        window = mock.Mock()
        api._webview_window = window

        with mock.patch.object(api_module.time, "sleep"), mock.patch.object(
            api_module.os, "_exit"
        ) as exit_now:
            api._quit_for_update()

        self.assertTrue(api._exiting_for_update)
        window.destroy.assert_called_once()
        exit_now.assert_called_once_with(0)


class LaunchPromptTests(unittest.TestCase):
    def test_a_found_update_asks_the_window_rather_than_opening_a_link(self):
        api = _api()
        window = mock.Mock()
        api._webview_window = window
        with mock.patch.object(
            api_module.updater, "latest_release", return_value=RELEASE
        ), mock.patch.object(
            api_module.updater,
            "describe_plan",
            return_value={"available": True, "latest": "v99.0"},
        ), mock.patch.object(
            api_module.updater, "is_frozen", return_value=True
        ):
            api.run_update_check()

        calls = " ".join(str(c) for c in window.evaluate_js.call_args_list)
        self.assertIn("show_update_prompt", calls)
        self.assertNotIn("open_link", calls)

    def test_no_update_means_no_prompt(self):
        api = _api()
        window = mock.Mock()
        api._webview_window = window
        with mock.patch.object(
            api_module.updater, "latest_release", return_value=RELEASE
        ), mock.patch.object(
            api_module.updater,
            "describe_plan",
            return_value={"available": False, "latest": "v99.0"},
        ):
            api.run_update_check()

        self.assertFalse(window.evaluate_js.called)


if __name__ == "__main__":
    unittest.main()


class FailureReportingTests(unittest.TestCase):
    """
    The helper runs after the window is gone, so the only way a failed update
    can be explained is a file the next start reads.

    Without this, a refused copy looked exactly like an update that was never
    offered: the app reopened on the old version and said nothing.
    """

    def _api(self, status_line):
        import tempfile

        api = _api()
        base = tempfile.mkdtemp()
        self.addCleanup(api_module.updater.clean_staging, base)
        target = os.path.join(base, "AutoRewarder")
        os.makedirs(target)
        if status_line is not None:
            with open(api_module.updater.update_status_path(target), "w") as handle:
                handle.write(status_line)
        return api, target

    def test_a_failed_update_is_reported_to_the_person(self):
        api, target = self._api(
            "failed: Windows would not replace the files (robocopy 8)"
        )
        window = mock.Mock()
        api._webview_window = window

        api._report_last_update(target)

        log = " ".join(api.logs)
        self.assertIn("did not install", log)
        self.assertIn("robocopy 8", log)
        calls = " ".join(str(c) for c in window.evaluate_js.call_args_list)
        self.assertIn("show_toast", calls)

    def test_an_installed_update_says_so_quietly(self):
        api, target = self._api("ok")
        window = mock.Mock()
        api._webview_window = window

        api._report_last_update(target)

        log = " ".join(api.logs)
        self.assertIn("Update installed", log)
        self.assertFalse(
            window.evaluate_js.called, "a success needs no dialog in front of anyone"
        )

    def test_the_status_is_read_once(self):
        api, target = self._api("ok")
        api._report_last_update(target)
        api.logs.clear()
        api._report_last_update(target)
        self.assertEqual(
            [line for line in api.logs if "Update installed" in line],
            [],
            "the same result must not be announced on every launch",
        )

    def test_no_status_means_nothing_to_say(self):
        api, target = self._api(None)
        api._report_last_update(target)
        self.assertEqual(api.logs, [])
