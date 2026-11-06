"""Regression tests for the classroom-only sequential demo preference."""

import os
import tempfile
import unittest
from unittest.mock import patch

from src.accounts import settings as settings_module


class TestSequentialDemoSetting(unittest.TestCase):
    def test_demo_setting_defaults_off_and_persists(self):
        with tempfile.TemporaryDirectory() as app_dir:
            settings_path = os.path.join(app_dir, "settings.json")
            with patch.object(settings_module, "APP_DIR", app_dir), patch.object(
                settings_module, "GLOBAL_SETTINGS_PATH", settings_path
            ):
                manager = settings_module.GlobalSettingsManager()
                self.assertFalse(manager.get_demo_sequence_all_accounts())

                manager.set_demo_sequence_all_accounts(True)
                self.assertTrue(manager.get_demo_sequence_all_accounts())

                saved = settings_module.GlobalSettingsManager()
                self.assertTrue(saved.get_demo_sequence_all_accounts())
                saved.set_demo_sequence_all_accounts(False)
                self.assertFalse(saved.get_demo_sequence_all_accounts())


if __name__ == "__main__":
    unittest.main()
