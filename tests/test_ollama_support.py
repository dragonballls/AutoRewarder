"""Unit tests for the optional local Ollama support client."""

import unittest
from unittest.mock import Mock, patch

from src.assistant.api_mixin import _redact_diagnostic_text
from src.assistant.ollama_support import OllamaSupport


class OllamaSupportTests(unittest.TestCase):
    def setUp(self):
        self.client = OllamaSupport()

    def test_only_loopback_urls_are_accepted(self):
        self.assertEqual(
            self.client._normalize_base_url("http://127.0.0.1:11434/"),
            "http://127.0.0.1:11434",
        )
        self.assertEqual(
            self.client._normalize_base_url("http://localhost:11434"),
            "http://localhost:11434",
        )
        with self.assertRaises(ValueError):
            self.client._normalize_base_url("https://example.com")
        with self.assertRaises(ValueError):
            self.client._normalize_base_url("http://192.0.2.10:11434")

    @patch("src.assistant.ollama_support.requests.get")
    def test_lists_installed_models(self, get):
        response = Mock()
        response.json.return_value = {
            "models": [
                {"name": "qwen3:4b"},
                {"name": "qwen3:8b"},
            ]
        }
        response.raise_for_status.return_value = None
        get.return_value = response

        result = self.client.list_models()

        self.assertTrue(result["ok"])
        self.assertEqual(
            [model["id"] for model in result["models"]],
            ["qwen3:4b", "qwen3:8b"],
        )
        get.assert_called_once_with(
            "http://127.0.0.1:11434/api/tags", timeout=4
        )

    @patch("src.assistant.ollama_support.requests.get")
    def test_remote_model_endpoint_is_rejected_before_network_call(self, get):
        result = self.client.list_models("http://example.com:11434")
        self.assertFalse(result["ok"])
        get.assert_not_called()

    @patch("src.assistant.ollama_support.requests.post")
    def test_chat_returns_local_model_answer(self, post):
        response = Mock()
        response.json.return_value = {
            "message": {"content": "Check that Ollama is running."}
        }
        response.raise_for_status.return_value = None
        post.return_value = response

        result = self.client.chat("Why is the model list empty?", "qwen3:4b")

        self.assertTrue(result["ok"])
        self.assertEqual(result["answer"], "Check that Ollama is running.")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "http://127.0.0.1:11434/api/chat")
        self.assertEqual(kwargs["json"]["model"], "qwen3:4b")
        self.assertFalse(kwargs["json"]["stream"])

    def test_diagnostic_logs_redact_credentials_and_email(self):
        source = (
            'api_key="secret-value" access_token=token-value '
            'Authorization: Bearer abc.def user@example.com'
        )
        result = _redact_diagnostic_text(source)
        self.assertNotIn("secret-value", result)
        self.assertNotIn("token-value", result)
        self.assertNotIn("abc.def", result)
        self.assertNotIn("user@example.com", result)
        self.assertIn("[EMAIL REDACTED]", result)


if __name__ == "__main__":
    unittest.main()
