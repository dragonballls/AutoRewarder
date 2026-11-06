"""Regression tests for the classroom-only sequential demo preference."""

import importlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


# Import this storage module without importing src/__init__.py, which eagerly
# loads the browser automation stack and is not needed for a settings test.
REPO_ROOT = Path(__file__).resolve().parents[2]
src_package = types.ModuleType("src")
src_package.__path__ = [str(REPO_ROOT / "src")]
sys.modules.setdefault("src", src_package)

accounts_package = types.ModuleType("src.accounts")
accounts_package.__path__ = [str(REPO_ROOT / "src" / "accounts")]
sys.modules.setdefault("src.accounts", accounts_package)

config_module = types.ModuleType("src.config")
setattr(config_module, "APP_DIR", "")
setattr(config_module, "GLOBAL_SETTINGS_PATH", "")
sys.modules.setdefault("src.config", config_module)

settings_module = importlib.import_module("src.accounts.settings")


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
