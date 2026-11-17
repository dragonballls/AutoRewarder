"""Console windows must never appear.

A windowed build has no console of its own, so Windows gives every console
program it starts — powershell, taskkill, schtasks, msedgedriver, a shell — a
fresh one. That window is the black rectangle that flashes on screen, and on a
machine someone is watching it reads as a fault. One helper decides the flags,
so one test can hold every caller to it.
"""

import os
import subprocess
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.utils import CREATE_NO_WINDOW, hidden_process_kwargs  # noqa: E402


class HiddenFlagsTests(unittest.TestCase):
    def test_windows_gets_create_no_window(self):
        flags = hidden_process_kwargs("Windows")
        self.assertEqual(flags, {"creationflags": CREATE_NO_WINDOW})
        # The value itself, not just "some flags": a wrong constant silently
        # does nothing and the window still appears.
        self.assertEqual(CREATE_NO_WINDOW, 0x08000000)

    def test_linux_gets_no_console_flag_it_cannot_use(self):
        """
        `creationflags` raises ValueError on Linux, so passing it there would
        break every subprocess the app starts.
        """
        flags = hidden_process_kwargs("Linux")
        self.assertNotIn("creationflags", flags)
        self.assertEqual(flags, {"start_new_session": True})

    def test_macos_gets_nothing_it_needs(self):
        self.assertEqual(hidden_process_kwargs("Darwin"), {})

    def test_the_running_platform_is_the_default(self):
        with mock.patch.object(sys, "platform", "win32"):
            self.assertIn("creationflags", hidden_process_kwargs())
        with mock.patch.object(sys, "platform", "linux"):
            self.assertIn("start_new_session", hidden_process_kwargs())
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(hidden_process_kwargs(), {})

    def test_the_flags_are_ones_subprocess_accepts_here(self):
        """
        Whatever this platform returns must be usable as-is: a bad keyword would
        only show up when a browser was being started at runtime.
        """
        result = subprocess.run(
            [sys.executable, "-c", "print('ok')"],
            capture_output=True,
            text=True,
            **hidden_process_kwargs(),
        )
        self.assertEqual(result.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
