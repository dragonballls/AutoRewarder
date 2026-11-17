"""Tests for using a local Ollama instance as an LLM provider."""

import unittest
from unittest.mock import MagicMock, patch

from src.search import llm


class OllamaSearchProviderTests(unittest.TestCase):
    @staticmethod
    def _session_mock():
        session = MagicMock()
        session.__enter__.return_value = session
        session.__exit__.return_value = False
        return session

    @patch("src.search.llm.requests.Session")
    def test_lists_installed_models_without_an_api_key(self, session_factory):
        session = self._session_mock()
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "models": [
                {"name": "qwen3:8b"},
                {"model": "qwen3:4b"},
            ]
        }
        session.get.return_value = response
        session_factory.return_value = session

        result = llm.list_models("ollama", "", "")

        self.assertTrue(result["ok"])
        self.assertEqual(
            [model["id"] for model in result["models"]],
            ["qwen3:4b", "qwen3:8b"],
        )
        self.assertFalse(session.trust_env)
        session.get.assert_called_once_with(
            "http://127.0.0.1:11434/api/tags", timeout=15
        )

    @patch("src.search.llm.requests.Session")
    def test_generates_queries_without_a_key_using_selected_model(
        self, session_factory
    ):
        session = self._session_mock()
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "message": {"content": '["weather today", "easy dinner recipes"]'}
        }
        session.post.return_value = response
        session_factory.return_value = session

        result = llm.generate_queries(
            2,
            "en-US",
            provider="ollama",
            model="qwen3:4b",
            api_key="",
        )

        self.assertEqual(result, ["weather today", "easy dinner recipes"])
        self.assertFalse(session.trust_env)
        args, kwargs = session.post.call_args
        self.assertEqual(args[0], "http://127.0.0.1:11434/api/chat")
        self.assertEqual(kwargs["json"]["model"], "qwen3:4b")
        self.assertFalse(kwargs["json"]["stream"])

    @patch("src.search.llm.requests.Session")
    def test_rejects_non_loopback_ollama_urls_before_network_call(
        self, session_factory
    ):
        result = llm.list_models("ollama", "", "http://192.168.1.20:11434")

        self.assertFalse(result["ok"])
        self.assertIn("local URL", result["error"])
        session_factory.assert_not_called()

    @patch("src.search.llm.requests.Session")
    def test_blank_model_uses_first_installed_local_model(self, session_factory):
        listing_session = self._session_mock()
        listing_response = MagicMock()
        listing_response.status_code = 200
        listing_response.json.return_value = {"models": [{"name": "qwen3:4b"}]}
        listing_session.get.return_value = listing_response

        chat_session = self._session_mock()
        chat_response = MagicMock()
        chat_response.status_code = 200
        chat_response.json.return_value = {
            "message": {"content": '["local model query"]'}
        }
        chat_session.post.return_value = chat_response
        session_factory.side_effect = [listing_session, chat_session]

        result = llm.generate_queries(
            1, "en-US", provider="ollama", model="", api_key=""
        )

        self.assertEqual(result, ["local model query"])
        args, kwargs = chat_session.post.call_args
        self.assertEqual(args[0], "http://127.0.0.1:11434/api/chat")
        self.assertEqual(kwargs["json"]["model"], "qwen3:4b")


if __name__ == "__main__":
    unittest.main()
