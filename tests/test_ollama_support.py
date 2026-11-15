"""Unit tests for the optional local Ollama support client."""

import unittest
from unittest.mock import MagicMock, patch

from src.assistant.api_mixin import _redact_diagnostic_text
from src.assistant.ollama_support import OllamaSupport


class OllamaSupportTests(unittest.TestCase):
    def setUp(self):
        self.client = OllamaSupport()

    @staticmethod
    def _session_mock():
        session = MagicMock()
        session.__enter__.return_value = session
        session.__exit__.return_value = False
        return session

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

    @patch("src.assistant.ollama_support.requests.Session")
    def test_lists_installed_models_without_system_proxy(self, session_factory):
        session = self._session_mock()
        response = MagicMock()
        response.json.return_value = {
            "models": [
                {"name": "qwen3:4b"},
                {"name": "qwen3:8b"},
            ]
        }
        response.raise_for_status.return_value = None
        session.get.return_value = response
        session_factory.return_value = session

        result = self.client.list_models()

        self.assertTrue(result["ok"])
        self.assertEqual(
            [model["id"] for model in result["models"]],
            ["qwen3:4b", "qwen3:8b"],
        )
        self.assertFalse(session.trust_env)
        session.get.assert_called_once_with(
            "http://127.0.0.1:11434/api/tags", timeout=4
        )

    @patch("src.assistant.ollama_support.requests.Session")
    def test_remote_model_endpoint_is_rejected_before_network_call(self, session_factory):
        result = self.client.list_models("http://example.com:11434")
        self.assertFalse(result["ok"])
        session_factory.assert_not_called()

    @patch("src.assistant.ollama_support.requests.Session")
    def test_chat_returns_local_model_answer_without_system_proxy(self, session_factory):
        session = self._session_mock()
        response = MagicMock()
        response.json.return_value = {
            "message": {"content": "Check that Ollama is running."}
        }
        response.raise_for_status.return_value = None
        session.post.return_value = response
        session_factory.return_value = session

        result = self.client.chat("Why is the model list empty?", "qwen3:4b")

        self.assertTrue(result["ok"])
        self.assertEqual(result["answer"], "Check that Ollama is running.")
        self.assertFalse(session.trust_env)
        args, kwargs = session.post.call_args
        self.assertEqual(args[0], "http://127.0.0.1:11434/api/chat")
        self.assertEqual(kwargs["json"]["model"], "qwen3:4b")
        self.assertFalse(kwargs["json"]["stream"])

    def test_diagnostic_logs_redact_credentials_and_email(self):
        source = (
            'api_key="secret-value" access_token=token-value '
            "Authorization: Bearer abc.def user@example.com account_id=12345"
        )
        result = _redact_diagnostic_text(source)
        self.assertNotIn("secret-value", result)
        self.assertNotIn("token-value", result)
        self.assertNotIn("abc.def", result)
        self.assertNotIn("user@example.com", result)
        self.assertNotIn("12345", result)
        self.assertIn("[EMAIL REDACTED]", result)


if __name__ == "__main__":
    unittest.main()
