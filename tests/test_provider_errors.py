"""Tests for quiet, actionable provider errors.

Real runs logged this on every startup, several times over:

    [WARNING] LLM (ollama) model list: network error: HTTPConnectionPool(
    host='localhost', port=11434) ... [WinError 10061] No connection could be
    made because the target machine actively refused it

That is not a fault — it is what "the local server isn't running" looks like.
Logging it as a warning on every launch trains people to ignore warnings, and
the traceback tells them nothing they can act on. The Settings UI already shows
the outcome, so a local provider that isn't running should say so there, once,
in words.
"""

import os
import sys
import unittest
from unittest import mock

import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.search import llm  # noqa: E402


def refused_error(host="127.0.0.1", port=11434):
    """The connection error Windows actually produced."""
    return requests.exceptions.ConnectionError(
        f"HTTPConnectionPool(host='{host}', port={port}): Max retries exceeded "
        f'with url: /api/tags (Caused by NewConnectionError("HTTPConnection('
        f"host='{host}', port={port}): Failed to establish a new connection: "
        "[WinError 10061] No connection could be made because the target "
        'machine actively refused it"))'
    )


class LocalProviderQuietTests(unittest.TestCase):
    def test_a_stopped_local_server_is_not_logged_as_a_warning(self):
        logs = []
        with mock.patch("requests.get", side_effect=refused_error()):
            result = llm.list_models(
                "openai-compatible",
                "key",
                "http://127.0.0.1:11434",
                logger=logs.append,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(logs, [], "an idle local server must not fill the run log")

    def test_the_message_says_what_to_do(self):
        with mock.patch("requests.get", side_effect=refused_error()):
            result = llm.list_models(
                "openai-compatible", "key", "http://127.0.0.1:11434"
            )

        message = result["error"]
        self.assertIn("No server is running", message)
        self.assertIn("Start it", message)
        # No Python internals in something a user reads.
        self.assertNotIn("WinError", message)
        self.assertNotIn("Traceback", message)
        self.assertNotIn("HTTPConnectionPool", message)

    def test_a_localhost_base_url_is_recognised(self):
        for url in (
            "http://localhost:11434",
            "http://127.0.0.1:11434",
            "http://[::1]:11434",
            "http://11434.localhost",
        ):
            self.assertTrue(llm._is_local_url(url), url)

    def test_a_remote_url_is_not_local(self):
        for url in ("https://api.openai.com", "http://192.168.1.10:8080", ""):
            self.assertFalse(llm._is_local_url(url), url)


class RemoteProviderStillWarnsTests(unittest.TestCase):
    def test_a_real_remote_outage_is_still_reported(self):
        """Quietening the local case must not hide a genuine failure."""
        logs = []
        with mock.patch(
            "requests.get", side_effect=refused_error("api.example.com", 443)
        ):
            result = llm.list_models(
                "openai-compatible",
                "key",
                "https://api.example.com",
                logger=logs.append,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(len(logs), 1, "a remote failure is worth a warning line")
        self.assertIn("network error", logs[0])

    def test_the_remote_message_stays_generic(self):
        with mock.patch(
            "requests.get", side_effect=refused_error("api.example.com", 443)
        ):
            result = llm.list_models(
                "openai-compatible", "key", "https://api.example.com"
            )
        self.assertEqual(result["error"], "Network error. Check your connection.")


class ConnectionRefusedDetectionTests(unittest.TestCase):
    def test_windows_refusal_is_detected(self):
        self.assertTrue(llm._is_connection_refused(refused_error()))

    def test_unix_refusal_is_detected(self):
        self.assertTrue(
            llm._is_connection_refused(
                requests.exceptions.ConnectionError("[Errno 111] Connection refused")
            )
        )

    def test_dns_failure_is_treated_as_unreachable(self):
        self.assertTrue(
            llm._is_connection_refused(
                requests.exceptions.ConnectionError(
                    "[Errno -2] Name or service not known"
                )
            )
        )

    def test_a_timeout_is_not_a_refusal(self):
        """A slow server is a different problem and keeps its own message."""
        self.assertFalse(
            llm._is_connection_refused(requests.exceptions.ConnectTimeout("timed out"))
        )


class ListModelsContractTests(unittest.TestCase):
    def test_listing_never_raises(self):
        """The Settings UI calls this; it must always return a result."""
        cases = [
            mock.patch("requests.get", side_effect=refused_error()),
            mock.patch("requests.get", side_effect=RuntimeError("odd")),
        ]
        for patcher in cases:
            with patcher:
                result = llm.list_models(
                    "openai-compatible", "key", "http://127.0.0.1:1"
                )
                self.assertIn("ok", result)
                self.assertFalse(result["ok"])

    def test_an_unsupported_provider_is_explained(self):
        result = llm.list_models("nonsense", "key")
        self.assertFalse(result["ok"])
        self.assertIn("Unsupported provider", result["error"])

    def test_a_missing_key_is_explained(self):
        result = llm.list_models("openai", "")
        self.assertFalse(result["ok"])
        self.assertIn("API key", result["error"])


if __name__ == "__main__":
    unittest.main()
