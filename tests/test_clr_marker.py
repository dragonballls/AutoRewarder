"""A downloaded build must still start: .NET needs its assemblies unblocked.

Windows marks files that arrive from a browser with a ``Zone.Identifier``
alternate data stream and propagates the mark to everything extracted from a
downloaded ZIP. The .NET Framework refuses to load a managed assembly carrying
that mark, so the shipped app died on launch — first on pythonnet's
``Python.Runtime.dll``::

    RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize

then, once that file was unblocked by hand, on WebView2::

    Could not load file or assembly '.../webview/lib/Microsoft.Web.WebView2.Core.dll'
    ... An attempt was made to load an assembly from a network location

and finally, with the whole ``_internal`` tree cleared, a marked *exe* still
failed before any Python ran. That last finding is why the fix is a launcher
(``scripts/Start-AutoRewarder.ps1``) plus an in-app pass for in-place updates.

These tests write real files, so they behave the same on Windows as elsewhere:
on a real Windows run the zone stream exists on disk, and the assertions are on
real behaviour rather than on a mocked filesystem.
"""

import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src import clr_support  # noqa: E402

IS_WINDOWS = sys.platform == "win32"

_BUNDLE_FILES = (
    "AutoRewarder-Ollama.exe",
    "pythonnet/runtime/Python.Runtime.dll",
    "clr_loader/ffi/dlls/amd64/ClrLoader.dll",
    "webview/lib/Microsoft.Web.WebView2.Core.dll",
    "webview/lib/Microsoft.Web.WebView2.WinForms.dll",
    "gui/index.html",
    "assets/queries.json",
)


def set_zone_marker(path):
    """Mark path as downloaded from the Internet, as a browser does."""
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"Set-Content -LiteralPath '{path}' "
            '-Stream Zone.Identifier -Value "[ZoneTransfer]`nZoneId=3`n" -NoNewline',
        ],
        check=True,
        capture_output=True,
    )


def read_streams(path):
    """Alternate data streams on path (empty off Windows)."""
    if not IS_WINDOWS:
        return []
    listing = os.popen(
        f"powershell -NoProfile -Command "
        f"\"try {{ (Get-Item -LiteralPath '{path}' -Stream *).Stream }} "
        f'catch {{ }}"'
    ).read()
    return [s.strip() for s in listing.split()]


class BundleFixture:
    """A temp tree shaped like the frozen app."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for relative in _BUNDLE_FILES:
            path = os.path.join(self.root, relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(b"contents")

    def path(self, relative):
        return os.path.join(self.root, relative)

    def cleanup(self):
        self.tmp.cleanup()


@unittest.skipUnless(IS_WINDOWS, "the download marker only exists on Windows")
class MarkerDiskTests(unittest.TestCase):
    """Real streams on disk. Skipped off Windows, where the mark cannot exist."""

    def setUp(self):
        self.fx = BundleFixture()

    def tearDown(self):
        self.fx.cleanup()

    def test_a_marked_dll_is_detected(self):
        target = self.fx.path("pythonnet/runtime/Python.Runtime.dll")
        set_zone_marker(target)
        self.assertIn("Zone.Identifier", read_streams(target))
        self.assertTrue(clr_support.has_zone_marker(target))

    def test_unblocking_removes_the_stream(self):
        target = self.fx.path("pythonnet/runtime/Python.Runtime.dll")
        set_zone_marker(target)
        cleared, still = clr_support.unblock_bundled_files(self.fx.root)
        self.assertIn(target, cleared)
        self.assertEqual(still, [])
        self.assertNotIn("Zone.Identifier", read_streams(target))

    def test_every_marked_file_is_cleared_including_both_known_failures(self):
        for relative in (
            "pythonnet/runtime/Python.Runtime.dll",
            "webview/lib/Microsoft.Web.WebView2.Core.dll",
            "AutoRewarder-Ollama.exe",
        ):
            set_zone_marker(self.fx.path(relative))
        cleared, still = clr_support.unblock_bundled_files(self.fx.root)
        self.assertEqual(still, [])
        self.assertEqual(len(cleared), 3)
        for relative in (
            "pythonnet/runtime/Python.Runtime.dll",
            "webview/lib/Microsoft.Web.WebView2.Core.dll",
            "AutoRewarder-Ollama.exe",
        ):
            self.assertNotIn("Zone.Identifier", read_streams(self.fx.path(relative)))

    def test_the_exe_beside_internal_is_covered_by_the_roots(self):
        # The real layout: exe and _internal/ are siblings, so sweeping only
        # _internal leaves a marked exe, which fails before Python starts.
        exe = self.fx.path("AutoRewarder-Ollama.exe")
        set_zone_marker(exe)
        cleared, _ = clr_support.unblock_bundled_files(self.fx.root)
        self.assertIn(exe, cleared)


class PureLogicTests(unittest.TestCase):
    """No filesystem: root resolution and messages, on any platform."""

    def test_off_windows_nothing_is_touched(self):
        fx = BundleFixture()
        try:
            with unittest.mock.patch.object(clr_support.sys, "platform", "linux"):
                self.assertEqual(clr_support.unblock_bundled_files(fx.root), ([], []))
                self.assertEqual(clr_support.unblocked_paths(fx.root), [])
        finally:
            fx.cleanup()

    def test_describe_unblock_is_quiet_when_there_was_nothing_to_do(self):
        self.assertIsNone(clr_support.describe_unblock([], []))

    def test_describe_unblock_reports_success(self):
        message = clr_support.describe_unblock(["a.dll"], [])
        self.assertIn("Unblocked 1", message)

    def test_describe_unblock_gives_an_actionable_failure(self):
        message = clr_support.describe_unblock([], ["a.dll"])
        self.assertIn("could not be unblocked", message)
        self.assertIn("Unblock", message)

    def test_roots_include_the_exe_directory(self):
        fx = BundleFixture()
        try:
            with unittest.mock.patch.object(
                sys, "frozen", True, create=True
            ), unittest.mock.patch.object(
                sys, "_MEIPASS", os.path.join(fx.root, "_internal"), create=True
            ), unittest.mock.patch.object(
                sys, "executable", os.path.join(fx.root, "AutoRewarder-Ollama.exe")
            ):
                roots = clr_support._roots()
            self.assertIn(fx.root, roots)
        finally:
            fx.cleanup()


class ShippedLauncherTests(unittest.TestCase):
    """The launcher is what actually fixes a freshly extracted download."""

    def _launcher(self):
        path = os.path.join(REPO_ROOT, "scripts", "Start-AutoRewarder.ps1")
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_the_launcher_ships_in_the_repository(self):
        self.assertTrue(
            os.path.isfile(os.path.join(REPO_ROOT, "scripts", "Start-AutoRewarder.ps1"))
        )

    def test_the_launcher_unblocks_before_it_launches(self):
        source = self._launcher()
        self.assertIn("Unblock-File", source)
        self.assertIn("Start-Process", source)
        self.assertLess(
            source.index("Unblock-File"),
            source.index("Start-Process"),
            "the marker must be cleared before the exe starts",
        )

    def test_the_launcher_refuses_to_launch_while_files_are_still_marked(self):
        source = self._launcher()
        self.assertIn("still marks", source)
        self.assertIn("throw", source)

    def test_the_build_packages_the_launcher_into_the_zip(self):
        with open(
            os.path.join(REPO_ROOT, ".github", "workflows", "build-windows.yml"),
            encoding="utf-8",
        ) as handle:
            workflow = handle.read()
        self.assertIn("Start-AutoRewarder.ps1", workflow)
        self.assertLess(
            workflow.index("Start-AutoRewarder.ps1"),
            workflow.index("Compress-Archive"),
            "the launcher has to be copied in before the archive is made",
        )

    def test_the_installer_unblocks_the_installed_copy(self):
        with open(
            os.path.join(REPO_ROOT, "scripts", "Install-AutoRewarder-Ollama.ps1"),
            encoding="utf-8",
        ) as handle:
            installer = handle.read()
        self.assertIn("Unblock-File", installer)
        self.assertLess(
            installer.index("Unblock-File"),
            installer.index("Start-Process -FilePath $installedExe"),
            "the installer must unblock before it launches",
        )


class StartupWiringTests(unittest.TestCase):
    """The in-app pass covers the in-place update path."""

    def setUp(self):
        with open(os.path.join(REPO_ROOT, "AutoRewarder.py"), encoding="utf-8") as f:
            self.source = f.read()

    def test_the_unblock_runs_before_webview_is_imported(self):
        marker = self.source.index("# GUI path")
        gui = self.source[marker:]
        self.assertIn("unblock_bundled_files()", gui)
        self.assertLess(
            gui.index("unblock_bundled_files()"),
            gui.index("\n    import webview"),
            "clr is imported by webview; the unblock must come first",
        )

    def test_the_headless_path_unblocks_before_the_cli_starts(self):
        headless_call = self.source.index("ensure_managed_runtime_loadable()")
        cli_import = self.source.index(
            "from AutoRewarder_CLI import main as headless_main"
        )
        self.assertLess(headless_call, cli_import)

    def test_the_gui_path_reports_what_it_did(self):
        self.assertIn("describe_unblock(_cleared, _still_marked)", self.source)
        self.assertIn("if unblock_message:", self.source)


if __name__ == "__main__":
    unittest.main()
