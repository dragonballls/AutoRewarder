"""Guard the integration hooks when original AutoRewarder changes."""

import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class IntegrationHookTests(unittest.TestCase):
    def test_core_api_uses_the_ollama_mixin(self):
        source = (ROOT / "src" / "api.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        api_class = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "AutoRewarderAPI"
        )
        bases = {
            base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
            for base in api_class.bases
        }
        self.assertIn("OllamaSupportMixin", bases)

        method_names = {
            node.name for node in api_class.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertIn("get_background_logs", method_names)

    def test_helper_bridge_methods_remain_available(self):
        source = (ROOT / "src" / "assistant" / "api_mixin.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        mixin = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "OllamaSupportMixin"
        )
        methods = {
            node.name for node in mixin.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertTrue({
            "open_ai_helper_window",
            "list_ollama_models",
            "ask_ollama_support",
        }.issubset(methods))

    def test_main_window_keeps_the_helper_entry_point(self):
        html = (ROOT / "gui" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "gui" / "script.js").read_text(encoding="utf-8")
        self.assertIn('id="ai_helper_btn"', html)
        self.assertIn("show_ai_helper()", html)
        self.assertIn("function show_ai_helper()", script)
        self.assertTrue((ROOT / "gui" / "ai_helper.html").is_file())


if __name__ == "__main__":
    unittest.main()
