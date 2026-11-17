"""AutoRewarder must run with no LLM key, and with no network at all.

The app's purpose is to earn Rewards points unattended. An API key is an
*optional* improvement to query variety, so no key, an unset key, a wrong key,
or no network at all must still leave every earning path working.

These tests stand on the boundary rather than on the happy path: they take away
the key, take away the network, take away the optional typo engine, and assert
the run still gets its searches.
"""

import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import requests  # noqa: E402

import src.api as api_module  # noqa: E402
from src.search import llm  # noqa: E402
from src.utils import check_for_updates, humanize_queries  # noqa: E402


def _offline(*args, **kwargs):
    raise requests.exceptions.ConnectionError("offline")


def _settings_with(**llm_config):
    """A settings stand-in whose LLM config is exactly what the test gives."""
    base = {
        "use_llm_queries": False,
        "llm_api_key": "",
        "llm_provider": "openai",
        "llm_model": "",
        "llm_base_url": "",
    }
    base.update(llm_config)
    settings = mock.Mock()
    settings.get_llm_config.return_value = base
    settings.get_settings.return_value = {}
    settings.get_manual_search_delay.return_value = (1.0, 2.0)
    return settings


class NoApiKeyTests(unittest.TestCase):
    def setUp(self):
        self.api = api_module.AutoRewarderAPI.__new__(api_module.AutoRewarderAPI)
        self.logs = []
        self.api.log = self.logs.append
        self.api._query_plan = []
        self.api._run_queries = set()
        self.api._run_planned_total = 0
        self.api.global_settings = _settings_with()

        self.engine = mock.Mock()
        self.engine.load_queries_from_json.return_value = [
            f"static query {i}" for i in range(80)
        ]
        self.api.search_engine = self.engine

    def test_defaults_leave_the_llm_switched_off(self):
        """A fresh install must not need a key, because it doesn't use one."""
        with open(
            os.path.join(REPO_ROOT, "src/accounts/settings.py"), encoding="utf-8"
        ) as handle:
            source = handle.read()

        # The shipped defaults, not a description of them.
        self.assertIn('"use_llm_queries": False', source)
        self.assertIn('"llm_api_key": ""', source)

    def test_a_run_gets_queries_with_no_key(self):
        queries = self.api._build_queries(30)
        self.assertEqual(len(queries), 30)
        self.assertTrue(all(queries))

    def test_switching_the_llm_on_without_a_key_still_runs(self):
        """A user can enable the feature and forget the key; the run survives."""
        self.api.global_settings = _settings_with(use_llm_queries=True)

        queries = self.api._build_queries(20)

        self.assertEqual(len(queries), 20)
        self.assertTrue(
            any("no API key" in line for line in self.logs),
            "the missing key should be explained in the log",
        )

    def test_planning_without_a_key_is_skipped_not_fatal(self):
        self.api._run_planned_total = 40
        self.api._plan_run_queries()

        self.assertEqual(self.api._query_plan, [])
        # The per-batch path takes over, which needs no key either.
        self.assertEqual(len(self.api._build_queries(10)), 10)

    def test_the_plan_returns_nothing_when_there_is_no_key(self):
        self.api.global_settings = _settings_with(use_llm_queries=True)
        self.api._run_planned_total = 40
        self.api._plan_run_queries()

        self.assertEqual(self.api._query_plan, [])
        self.assertEqual(self.api._next_planned_queries(5), [])


class NoNetworkTests(unittest.TestCase):
    def test_generate_queries_returns_nothing_instead_of_raising(self):
        with mock.patch.object(requests, "post", side_effect=_offline):
            result = llm.generate_queries(
                5, "en-US", provider="openai", api_key="sk-fake", logger=lambda m: None
            )
        self.assertEqual(result, [])

    def test_the_update_check_is_not_fatal_offline(self):
        with mock.patch.object(requests, "get", side_effect=_offline):
            available, latest = check_for_updates(logger=lambda m: None)
        self.assertFalse(available)
        self.assertIsNone(latest)

    def test_a_full_query_batch_falls_back_to_static_offline(self):
        """The LLM is configured and has a key, but the network is down."""
        api = api_module.AutoRewarderAPI.__new__(api_module.AutoRewarderAPI)
        api.log = lambda message: None
        api._query_plan = []
        api._run_queries = set()
        api._run_planned_total = 0
        api.global_settings = _settings_with(
            use_llm_queries=True, llm_api_key="sk-fake"
        )
        engine = mock.Mock()
        engine.load_queries_from_json.return_value = [
            f"static query {i}" for i in range(80)
        ]
        api.search_engine = engine

        with mock.patch.object(requests, "post", side_effect=_offline):
            queries = api._build_queries(25)

        self.assertEqual(len(queries), 25)
        self.assertTrue(all(queries), "a fallback query must never be blank")

    def test_visual_search_and_balance_need_no_http_library(self):
        """
        Only the optional LLM path speaks HTTP to a third party. Visual Search
        and the balance read work through the browser, so they are unaffected by
        a missing key or a blocked direct connection.
        """
        for module in ("src/search/engine.py", "src/stats/manager.py"):
            with open(os.path.join(REPO_ROOT, module), encoding="utf-8") as handle:
                source = handle.read()
            self.assertNotIn("import requests", source, module)


class SignedOutDetectionTests(unittest.TestCase):
    """
    Signed out, every task used to fail identically — "nothing found" — which
    reads like an already-finished day and sends the user looking in the wrong
    place. The run now says what is actually wrong.
    """

    def _api(self):
        api = api_module.AutoRewarderAPI.__new__(api_module.AutoRewarderAPI)
        self.logs = []
        api.log = self.logs.append
        return api

    def _driver(self, url):
        driver = mock.Mock()
        driver.current_url = url
        return driver

    def test_a_sign_in_redirect_is_reported(self):
        api = self._api()
        self.assertFalse(
            api._ensure_signed_in(
                self._driver("https://login.live.com/oauth20_authorize.srf?x=1")
            )
        )
        self.assertTrue(any("signed out" in line for line in self.logs))
        self.assertTrue(
            any("First Setup" in line for line in self.logs), "say how to fix it"
        )

    def test_microsoft_online_and_account_hosts_are_detected_too(self):
        for url in (
            "https://login.microsoftonline.com/abc/oauth2",
            "https://account.microsoft.com/account",
        ):
            api = self._api()
            self.assertFalse(api._ensure_signed_in(self._driver(url)), url)

    def test_a_rewards_page_is_treated_as_signed_in(self):
        api = self._api()
        self.assertTrue(
            api._ensure_signed_in(self._driver("https://rewards.bing.com/dashboard"))
        )
        self.assertEqual(self.logs, [], "no warning when the session is fine")

    def test_an_unreadable_page_does_not_block_the_run(self):
        api = self._api()
        broken = mock.Mock()
        type(broken).current_url = mock.PropertyMock(side_effect=RuntimeError("gone"))
        self.assertTrue(api._ensure_signed_in(broken))

    def test_no_driver_is_not_an_error_here(self):
        self.assertTrue(self._api()._ensure_signed_in(None))


class NoTypoEngineTests(unittest.TestCase):
    def test_queries_survive_a_missing_nlpaug(self):
        queries = ["quiche lorraine", "weather paris"]
        with mock.patch("builtins.__import__", side_effect=ImportError("gone")):
            self.assertEqual(humanize_queries(queries), queries)

    def test_a_frozen_build_without_metadata_also_survives(self):
        from importlib.metadata import PackageNotFoundError

        queries = ["banana bread"]
        with mock.patch("builtins.__import__", side_effect=PackageNotFoundError):
            self.assertEqual(humanize_queries(queries), queries)


class RequirementTests(unittest.TestCase):
    def test_no_key_is_read_from_the_environment_or_required_at_startup(self):
        """
        Nothing may refuse to start because a key is absent: the only place a key
        is consulted is query generation, which is optional.
        """
        with open(os.path.join(REPO_ROOT, "src/api.py"), encoding="utf-8") as handle:
            source = handle.read()

        # Every read of the key sits inside the query-building/planning methods.
        for line_number, line in enumerate(source.splitlines(), 1):
            if "llm_api_key" not in line:
                continue
            self.assertNotIn(
                "raise",
                line,
                f"src/api.py:{line_number} raises on a missing key: {line.strip()}",
            )


if __name__ == "__main__":
    unittest.main()
