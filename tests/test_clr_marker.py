"""A downloaded build must still start: .NET needs its assemblies unblocked.

Windows marks files that arrive from a browser with a ``Zone.Identifier``
alternate data stream and propagates the mark to everything extracted from a
downloaded ZIP. The .NET Framework refuses to load a managed assembly carrying
that mark, so the shipped app died on launch — first on pythonnet's
``Python.Runtime.dll``::

    RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize

and, once that one file was unblocked by hand, again on WebView2::

    Could not load file or assembly '.../webview/lib/Microsoft.Web.WebView2.Core.dll'
    ... An attempt was made to load an assembly from a network location

That second failure is why the fix cannot list DLLs to clear: the bundle holds
dozens of assemblies and which ones the CLR loads depends on the backend. These
tests pin that the whole bundle is cleared, on both entry paths.
"""

import ast
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src import clr_support  # noqa: E402


class BundleFixture:
    """A temp tree shaped like the frozen app, with optional marks."""

    FILES = (
        "pythonnet/runtime/Python.Runtime.dll",
        "clr_loader/ffi/dlls/amd64/ClrLoader.dll",
        "webview/lib/Microsoft.Web.WebView2.Core.dll",
        "webview/lib/Microsoft.Web.WebView2.WinForms.dll",
        "gui/index.html",
        "assets/queries.json",
    )

    def __init__(self, marked=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for relative in self.FILES:
            path = os.path.join(self.root, relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(b"contents")
        self.store = {}
        for relative in marked or ():
            self.store[self.path(relative) + ":" + clr_support._ZONE_STREAM] = (
                b"[ZoneTransfer]\r\nZoneId=3\r\n"
            )

    def path(self, relative):
        return os.path.join(self.root, relative)

    def cleanup(self):
        self.tmp.cleanup()


def _windows_fs(fixture):
    """Context manager making the zone-stream lookups hit fixture.store."""
    real_open = open

    def fake_open(path, mode="r", *args, **kwargs):
        if isinstance(path, str) and path in fixture.store:
            return io.BytesIO(fixture.store[path])
        return real_open(path, mode, *args, **kwargs)

    def fake_remove(path):
        if path not in fixture.store:
            raise OSError("no such stream")
        fixture.store.pop(path)

    return mock.patch.multiple(
        clr_support,
        **{
            "sys": mock.MagicMock(platform="win32"),
            "os": _patched_os(fake_remove),
        },
    ), mock.patch("builtins.open", side_effect=fake_open)


def _patched_os(fake_remove):
    class _Os:
        def __getattr__(self, name):
            return getattr(os, name)

        remove = staticmethod(fake_remove)

    return _Os()


class WholeBundleIsClearedTests(unittest.TestCase):
    def test_pythonnet_assembly_is_cleared(self):
        fx = BundleFixture(marked=["pythonnet/runtime/Python.Runtime.dll"])
        try:
            with _windows_fs(fx)[0], _windows_fs(fx)[1]:
                cleared, still = clr_support.unblock_bundled_files(fx.root)
            self.assertEqual(cleared, [fx.path("pythonnet/runtime/Python.Runtime.dll")])
            self.assertEqual(still, [])
        finally:
            fx.cleanup()

    def test_webview2_assembly_is_cleared_too(self):
        # The second failure a user hit, after pythonnet was unblocked by hand.
        fx = BundleFixture(marked=["webview/lib/Microsoft.Web.WebView2.Core.dll"])
        try:
            with _windows_fs(fx)[0], _windows_fs(fx)[1]:
                cleared, still = clr_support.unblock_bundled_files(fx.root)
            self.assertEqual(
                cleared, [fx.path("webview/lib/Microsoft.Web.WebView2.Core.dll")]
            )
            self.assertEqual(still, [])
        finally:
            fx.cleanup()

    def test_every_marked_file_is_cleared_not_a_hardcoded_list(self):
        marked = [
            "pythonnet/runtime/Python.Runtime.dll",
            "clr_loader/ffi/dlls/amd64/ClrLoader.dll",
            "webview/lib/Microsoft.Web.WebView2.Core.dll",
            "webview/lib/Microsoft.Web.WebView2.WinForms.dll",
        ]
        fx = BundleFixture(marked=marked)
        try:
            with _windows_fs(fx)[0], _windows_fs(fx)[1]:
                cleared, still = clr_support.unblock_bundled_files(fx.root)
            self.assertEqual(sorted(cleared), sorted(fx.path(m) for m in marked))
            self.assertEqual(still, [])
            self.assertEqual(fx.store, {}, "no zone stream may survive")
        finally:
            fx.cleanup()

    def test_unmarked_bundle_changes_nothing(self):
        fx = BundleFixture()
        try:
            with _windows_fs(fx)[0], _windows_fs(fx)[1]:
                cleared, still = clr_support.unblock_bundled_files(fx.root)
            self.assertEqual((cleared, still), ([], []))
        finally:
            fx.cleanup()

    def test_non_windows_is_a_no_op(self):
        fx = BundleFixture(marked=["pythonnet/runtime/Python.Runtime.dll"])
        try:
            with mock.patch.object(clr_support.sys, "platform", "linux"):
                cleared, still = clr_support.unblock_bundled_files(fx.root)
            self.assertEqual((cleared, still), ([], []))
            self.assertEqual(len(fx.store), 1, "nothing may be touched off Windows")
        finally:
            fx.cleanup()

    def test_a_read_only_file_is_reported_not_swallowed(self):
        fx = BundleFixture(marked=["pythonnet/runtime/Python.Runtime.dll"])
        try:
            real_open = open

            def fake_open(path, mode="r", *a, **k):
                if isinstance(path, str) and path in fx.store:
                    return io.BytesIO(fx.store[path])
                return real_open(path, mode, *a, **k)

            with mock.patch.object(clr_support.sys, "platform", "win32"), mock.patch(
                "builtins.open", side_effect=fake_open
            ), mock.patch.object(
                clr_support.os, "remove", side_effect=PermissionError
            ), mock.patch.object(
                clr_support, "unblocked_paths", return_value=list(fx.store)
            ):
                cleared, still = clr_support.unblock_bundled_files(fx.root)
            self.assertEqual(cleared, [])
            self.assertEqual(len(still), 1, "the user must be told it is still blocked")
        finally:
            fx.cleanup()

    def test_read_only_install_produces_an_actionable_message(self):
        message = clr_support.describe_unblock([], ["a.dll", "b.dll"])
        self.assertIn("could not be unblocked", message)
        self.assertIn("Unblock", message)

    def test_nothing_to_do_means_no_message(self):
        self.assertIsNone(clr_support.describe_unblock([], []))

    def test_successful_clear_is_reported(self):
        message = clr_support.describe_unblock(["a.dll"], [])
        self.assertIn("Unblocked 1", message)

    def test_bundle_root_follows_pyinstaller(self):
        fx = BundleFixture()
        try:
            with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
                sys, "_MEIPASS", fx.root, create=True
            ):
                self.assertEqual(clr_support._bundle_root(), fx.root)
        finally:
            fx.cleanup()

    def test_bundle_root_falls_back_next_to_the_exe(self):
        fx = BundleFixture()
        try:
            with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
                sys, "_MEIPASS", None, create=True
            ), mock.patch.object(sys, "executable", os.path.join(fx.root, "app.exe")):
                self.assertEqual(clr_support._bundle_root(), fx.root)
        finally:
            fx.cleanup()


class StartupWiringTests(unittest.TestCase):
    """The unblock must happen before pywebview can import clr."""

    def setUp(self):
        with open(os.path.join(REPO_ROOT, "AutoRewarder.py"), encoding="utf-8") as f:
            self.source = f.read()

    def test_the_unblock_runs_before_webview_is_imported(self):
        # Scope to the GUI path: the headless branch has its own earlier call.
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

    def test_the_unblock_is_not_a_list_of_hardcoded_dlls(self):
        # The WebView2 failure is what proves a fixed list cannot work.
        tree = ast.parse(self.source)
        self.assertTrue(tree is not None)
        self.assertNotIn("_MANAGED_DLLS", self.source)


if __name__ == "__main__":
    unittest.main()
