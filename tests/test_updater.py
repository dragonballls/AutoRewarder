"""Tests for the in-app updater.

The updater is the whole reason someone stops downloading by hand, so it has to
be right. It was wrong in two ways that each guaranteed it would never deliver:

  * it read releases from `safarsin/AutoRewarder`, which is upstream — a release
    that does not contain these features, and whose version equals ours, so it
    reported "up to date" forever;
  * it compared version strings with `!=`, so a *lower* published version was
    offered as an update, and "v4.10" compared as older than "v4.9".
"""

import os
import sys
import unittest
from unittest import mock

import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src import config  # noqa: E402
from src.utils import _version_tuple, check_for_updates, is_newer_version  # noqa: E402


class VersionOrderingTests(unittest.TestCase):
    def test_a_newer_version_is_an_update(self):
        self.assertTrue(is_newer_version("v4.5", "v4.4"))

    def test_the_same_version_is_not_an_update(self):
        self.assertFalse(is_newer_version("v4.4", "v4.4"))
        self.assertFalse(is_newer_version("4.4", "v4.4"))

    def test_an_older_published_version_is_not_offered(self):
        """Downgrading must never be presented as an upgrade."""
        self.assertFalse(is_newer_version("v4.3", "v4.4"))

    def test_double_digits_order_numerically(self):
        """The bug a string compare has: "4.10" is newer than "4.9"."""
        self.assertTrue(is_newer_version("v4.10", "v4.9"))
        self.assertFalse(is_newer_version("v4.9", "v4.10"))

    def test_patch_versions_are_compared(self):
        self.assertTrue(is_newer_version("v4.5.1", "v4.5"))
        self.assertFalse(is_newer_version("v4.5", "v4.5.1"))

    def test_a_major_jump_wins(self):
        self.assertTrue(is_newer_version("v5.0", "v4.99"))

    def test_a_suffix_does_not_break_parsing(self):
        self.assertTrue(is_newer_version("v4.5-beta", "v4.4"))
        self.assertEqual(_version_tuple("v4.5-beta"), (4, 5, 0))

    def test_unparseable_versions_never_win(self):
        self.assertFalse(is_newer_version("", "v4.4"))
        self.assertFalse(is_newer_version(None, "v4.4"))
        self.assertFalse(is_newer_version("latest", "v4.4"))


class UpdaterTargetTests(unittest.TestCase):
    def test_releases_are_read_from_this_edition_not_upstream(self):
        """
        Upstream publishes v4.4 without these features. Reading its releases
        means the updater can never see a build that has them.
        """
        self.assertEqual(config.REPO, "dragonballls/AutoRewarder")
        self.assertNotEqual(config.REPO, "safarsin/AutoRewarder")

    def test_the_reported_version_has_moved_past_upstream(self):
        """Otherwise the comparison finds nothing newer than what we ship."""
        self.assertTrue(
            _version_tuple(config.CURRENT_VERSION) > _version_tuple("v4.4"),
            f"{config.CURRENT_VERSION} must be newer than upstream v4.4",
        )


class CheckForUpdatesTests(unittest.TestCase):
    def _response(self, status=200, tag=None):
        response = mock.Mock()
        response.status_code = status
        response.json.return_value = {"tag_name": tag}
        response.headers = {}
        return response

    def test_a_newer_release_is_reported(self):
        with mock.patch.object(
            requests, "get", return_value=self._response(tag="v9.9")
        ):
            available, latest = check_for_updates()
        self.assertTrue(available)
        self.assertEqual(latest, "v9.9")

    def test_the_current_release_is_not_reported(self):
        with mock.patch.object(
            requests, "get", return_value=self._response(tag=config.CURRENT_VERSION)
        ):
            available, latest = check_for_updates()
        self.assertFalse(available)

    def test_an_older_release_is_not_reported(self):
        with mock.patch.object(
            requests, "get", return_value=self._response(tag="v4.0")
        ):
            available, _ = check_for_updates()
        self.assertFalse(available)

    def test_it_asks_this_edition_for_its_latest_release(self):
        with mock.patch.object(
            requests, "get", return_value=self._response(tag="v9.9")
        ) as get:
            check_for_updates()
        url = get.call_args[0][0]
        self.assertIn(f"repos/{config.REPO}/releases/latest", url)

    def test_being_offline_is_not_an_error(self):
        with mock.patch.object(
            requests, "get", side_effect=requests.exceptions.ConnectionError("offline")
        ):
            available, latest = check_for_updates()
        self.assertFalse(available)
        self.assertIsNone(latest)

    def test_a_missing_release_is_not_an_error(self):
        with mock.patch.object(
            requests, "get", return_value=self._response(status=404)
        ):
            available, latest = check_for_updates()
        self.assertFalse(available)
        self.assertIsNone(latest)


class PackagingContractTests(unittest.TestCase):
    """
    The download and the updater have to agree on names, or the release is
    published but uninstallable.
    """

    def test_the_build_workflow_publishes_a_release(self):
        with open(
            os.path.join(REPO_ROOT, ".github/workflows/build-release.yml"),
            encoding="utf-8",
        ) as handle:
            workflow = handle.read()

        # A release is what the updater reads; an artifact alone is invisible.
        self.assertIn("action-gh-release", workflow)
        # The tag must be a version the app can compare against. See
        # ReleaseTagTests: a release named "latest" can never be an update.
        self.assertIn("tag_name: ${{ steps.version.outputs.version }}", workflow)

    def test_the_release_carries_the_zip_the_launcher_expects(self):
        with open(
            os.path.join(REPO_ROOT, ".github/workflows/build-release.yml"),
            encoding="utf-8",
        ) as handle:
            workflow = handle.read()
        self.assertIn("AutoRewarder-windows.zip", workflow)
        self.assertIn("Start-AutoRewarder.ps1", workflow)
        # Both platforms are published together, so neither user is left with a
        # release that has no download for them.
        self.assertIn("AutoRewarder-linux.tar.gz", workflow)

    def test_the_launcher_ships_with_the_build(self):
        launcher = os.path.join(REPO_ROOT, "scripts", "Start-AutoRewarder.ps1")
        self.assertTrue(
            os.path.isfile(launcher), "a downloaded build cannot start without it"
        )
        with open(launcher, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("AutoRewarder.exe", text)
        self.assertIn("Unblock-File", text)

    def test_the_tests_run_before_a_release_is_published(self):
        with open(
            os.path.join(REPO_ROOT, ".github/workflows/build-release.yml"),
            encoding="utf-8",
        ) as handle:
            workflow = handle.read()
        self.assertIn("unittest discover", workflow)
        # The test step must come before the release step in the file.
        self.assertLess(
            workflow.index("unittest discover"),
            workflow.index("action-gh-release"),
            "a release must not be published without the suite passing",
        )
        # The release job depends on both builds, so a half-finished release
        # cannot be published.
        self.assertIn("needs: [build-windows, build-linux]", workflow)


class ReleaseBlockingTests(unittest.TestCase):
    """
    A build job that cannot succeed withholds the release from everyone, because
    publishing waits for it. These lock in the two ways the Linux job could not.
    """

    def _workflow(self):
        with open(
            os.path.join(REPO_ROOT, ".github/workflows/build-release.yml"),
            encoding="utf-8",
        ) as handle:
            return handle.read()

    @staticmethod
    def _dependency_lines(text):
        """The declared requirements, without comments or blank lines."""
        return [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def _requirements(self):
        with open(
            os.path.join(REPO_ROOT, "requirements.txt"), encoding="utf-8"
        ) as handle:
            return "\n".join(self._dependency_lines(handle.read()))

    def _linux_requirements(self):
        with open(
            os.path.join(REPO_ROOT, "requirements-linux.txt"), encoding="utf-8"
        ) as handle:
            return "\n".join(self._dependency_lines(handle.read()))

    def test_the_qt_binding_pywebview_imports_is_declared(self):
        """
        `webview.start(gui="qt")` reaches Qt through qtpy, and qtpy needs a
        binding to find. Neither was declared for Linux, so a packaged Linux
        build could not import the backend it opens its window with.
        """
        linux = self._linux_requirements()
        self.assertIn("PySide6", linux)
        self.assertIn("QtPy", linux)

    def test_pygobject_is_not_required(self):
        """
        Nothing imports GTK: the window is Qt. PyGObject also builds from source
        against system GObject headers a bare runner lacks, so requiring it
        failed the install and took the release with it.
        """
        self.assertNotIn("PyGObject", self._requirements())
        self.assertNotIn("PyGObject", self._linux_requirements())

    def test_no_workflow_edits_requirements_in_place(self):
        """
        Every workflow used to delete lines from requirements.txt before
        installing, which mutated a tracked file and made the checks depend on
        the mutation instead of on what the repository says.
        """
        for name in ("lint.yml", "build-release.yml", "sync-upstream.yml"):
            with open(
                os.path.join(REPO_ROOT, ".github/workflows", name), encoding="utf-8"
            ) as handle:
                text = handle.read()
            self.assertNotIn("sed -i", text, f"{name} edits files in place")

    def test_the_linux_build_installs_the_gui_stack(self):
        """A Linux build without its GUI bindings opens no window."""
        self.assertIn("requirements-linux.txt", self._workflow())

    def test_the_qt_backend_is_checked_during_the_build(self):
        """A broken backend should fail the build, not the user's first run."""
        self.assertIn("import webview.platforms.qt", self._workflow())

    def test_a_linux_failure_does_not_withhold_the_windows_download(self):
        """The download most people want must not depend on the other platform."""
        workflow = self._workflow()
        self.assertIn("needs.build-windows.result == 'success'", workflow)
        self.assertIn("continue-on-error: true", workflow)

    def test_the_release_fails_when_there_is_no_windows_download(self):
        """Guard against publishing a release whose only asset is missing."""
        self.assertIn("no Windows download to publish", self._workflow())


class ReleaseTagTests(unittest.TestCase):
    """
    The app compares its own version against the published release's *tag*. A
    release tagged with anything that is not a version therefore reads as older
    than the app, and the update check reports "up to date" forever.
    """

    def _workflow(self):
        with open(
            os.path.join(REPO_ROOT, ".github/workflows/build-release.yml"),
            encoding="utf-8",
        ) as handle:
            return handle.read()

    def test_a_plain_name_tag_would_never_be_an_update(self):
        """Pinning `tag_name: latest` is the failure, not a shortcut."""
        self.assertFalse(is_newer_version("latest", "v4.5"))
        self.assertEqual(_version_tuple("latest"), (0, 0, 0))

    def test_the_release_is_tagged_with_the_version_the_app_reports(self):
        workflow = self._workflow()
        self.assertIn("tag_name: ${{ steps.version.outputs.version }}", workflow)
        self.assertNotIn("tag_name: latest", workflow)

    def test_the_tag_is_read_from_the_single_source_of_truth(self):
        """A tag typed into the workflow would drift from the app's version."""
        self.assertIn("src/config.py", self._workflow())

    def test_the_release_tag_round_trips_as_an_updatable_version(self):
        """
        Publishing at the app's current version must then read as up to date,
        and the next version must read as an update — the whole cycle.
        """
        self.assertFalse(
            is_newer_version(config.CURRENT_VERSION, config.CURRENT_VERSION)
        )
        self.assertTrue(
            is_newer_version(
                f"v{_version_tuple(config.CURRENT_VERSION)[0]}.99",
                config.CURRENT_VERSION,
            )
        )


if __name__ == "__main__":
    unittest.main()
