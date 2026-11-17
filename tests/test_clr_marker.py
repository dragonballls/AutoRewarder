"""A downloaded build must still start: the CLR needs its assemblies unblocked.

Windows marks files that arrive from a browser with a ``Zone.Identifier``
alternate data stream and propagates the mark to everything extracted from a
downloaded ZIP. The .NET Framework refuses to load a managed assembly carrying
that mark, so the shipped app died on launch with::

    RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize
    from ...\\_internal\\pythonnet\\runtime\\Python.Runtime.dll

which is what a user gets from simply extracting the release and double-clicking.
These tests pin the fix: the app clears the mark on its own bundled assemblies
before anything imports ``clr``.
"""

import ast
import os
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src import clr_support  # noqa: E402


class ZoneMarkerFixture:
    """A temp 'bundle' laid out like the frozen app."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for relative in clr_support._MANAGED_DLLS:
            path = os.path.join(self.root, relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(b"MZ fake assembly")

    def path(self, relative):
        return os.path.join(self.root, relative)

    def cleanup(self):
        self.tmp.cleanup()


class MarkerClearingTests(unittest.TestCase):
    def setUp(self):
        self.fx = ZoneMarkerFixture()

    def tearDown(self):
        self.fx.cleanup()

    def test_every_bundled_assembly_is_considered(self):
        # The traceback names Python.Runtime.dll; the native host shim can carry
        # the mark too, so both must be handled.
        joined = " ".join(clr_support._MANAGED_DLLS)
        self.assertIn("Python.Runtime.dll", joined)
        self.assertIn("ClrLoader.dll", joined)

    def test_a_marker_on_the_runtime_dll_is_removed(self):
        target = self.fx.path(clr_support._MANAGED_DLLS[0])
        marker = target + ":" + clr_support._ZONE_STREAM

        # Model Windows' alternate data stream without needing Windows: the
        # stream "exists" only while the store says so. os.path.isfile stays
        # real by patching open only for the stream path.
        store = {marker: b"[ZoneTransfer]\r\nZoneId=3\r\n"}
        real_open = open

        def fake_open(path, mode="r", *args, **kwargs):
            if path in store:
                import io

                return io.BytesIO(store[path])
            return real_open(path, mode, *args, **kwargs)

        def fake_remove(path):
            if path not in store:
                raise OSError("no such stream")
            store.pop(path)

        with mock.patch.object(clr_support.sys, "platform", "win32"), mock.patch(
            "builtins.open", side_effect=fake_open
        ), mock.patch.object(
            clr_support.os, "remove", side_effect=fake_remove
        ), mock.patch.object(
            clr_support, "_bundle_root", return_value=self.fx.root
        ):
            cleared = clr_support.unblock_bundled_assemblies()

        self.assertIn(target, cleared)
        self.assertNotIn(marker, store, "the Zone.Identifier stream must be gone")

    def test_an_unmarked_bundle_changes_nothing(self):
        with mock.patch.object(clr_support.sys, "platform", "win32"), mock.patch(
            "builtins.open", side_effect=OSError("no such stream")
        ), mock.patch.object(clr_support.os, "remove") as remove:
            cleared = clr_support.unblock_bundled_assemblies()

        self.assertEqual(cleared, [])
        remove.assert_not_called()

    def test_non_windows_is_a_no_op(self):
        with mock.patch.object(clr_support.sys, "platform", "linux"), mock.patch.object(
            clr_support.os, "remove"
        ) as remove:
            cleared = clr_support.unblock_bundled_assemblies()

        self.assertEqual(cleared, [])
        remove.assert_not_called()

    def test_a_read_only_location_does_not_raise(self):
        with mock.patch.object(clr_support.sys, "platform", "win32"), mock.patch(
            "builtins.open", side_effect=lambda *a, **k: mock.mock_open(read_data=b"x")
        ), mock.patch.object(clr_support.os, "remove", side_effect=PermissionError):
            # Must degrade to "nothing cleared", not crash the startup path.
            self.assertEqual(clr_support.unblock_bundled_assemblies(), [])

    def test_the_bundle_root_follows_pyinstaller(self):
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
            sys, "_MEIPASS", self.fx.root, create=True
        ):
            self.assertEqual(clr_support._bundle_root(), self.fx.root)

    def test_the_bundle_root_falls_back_next_to_the_exe(self):
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
            sys, "_MEIPASS", None, create=True
        ), mock.patch.object(sys, "executable", os.path.join(self.fx.root, "app.exe")):
            self.assertEqual(clr_support._bundle_root(), self.fx.root)


class StartupWiringTests(unittest.TestCase):
    """The unblock must happen before pywebview can import clr."""

    def _entry_source(self):
        with open(os.path.join(REPO_ROOT, "AutoRewarder.py"), encoding="utf-8") as f:
            return f.read()

    def test_the_unblock_runs_before_webview_is_imported(self):
        source = self._entry_source()
        self.assertIn("ensure_managed_runtime_loadable()", source)
        self.assertLess(
            source.index("ensure_managed_runtime_loadable()"),
            source.index("import webview"),
            "clr is imported by webview; the unblock must come first",
        )

    def test_the_headless_path_unblocks_too(self):
        tree = ast.parse(self._entry_source())
        # The headless branch runs before the GUI import; assert the call sits
        # in the same source region as the CLI import it precedes.
        source = self._entry_source()
        headless = source.index("headless_main()")
        cli_import = source.index("from AutoRewarder_CLI import main as headless_main")
        self.assertIn("ensure_managed_runtime_loadable()", source[:headless])
        self.assertLess(source.index("ensure_managed_runtime_loadable()"), cli_import)
        self.assertTrue(tree is not None)


if __name__ == "__main__":
    unittest.main()
