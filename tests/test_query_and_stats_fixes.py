"""Regression tests for the query-generation and stats fixes.

Covers:

  * issue #160 — a missing/unusable nlpaug must not abort a run. The typo pass
    sits on the static-query fallback, so raising there lost every search of a
    run whose LLM call had merely failed.
  * issue #159 — advanced scheduling asked the LLM for one query per batch, so
    the day collapsed onto a repeated topic. The run's queries are now planned
    up front in larger, mutually-exclusive batches.
  * issue #161 — redeeming points made the "earned this session" figure go
    negative. The spend is recorded, but never as negative earnings.
"""

import os
import sys
import tempfile
import unittest
from importlib.metadata import PackageNotFoundError
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import src.search.llm as llm  # noqa: E402
from src.stats.manager import StatsManager  # noqa: E402
from src.utils import humanize_queries  # noqa: E402


class HumanizeQueriesFallbackTests(unittest.TestCase):
    """Issue #160: the fallback must degrade, never raise."""

    def test_unavailable_nlpaug_returns_queries_unchanged(self):
        queries = ["quiche lorraine", "weather paris"]
        with mock.patch("builtins.__import__", side_effect=ImportError("no nlpaug")):
            result = humanize_queries(queries)
        self.assertEqual(result, queries)

    def test_unavailable_nlpaug_is_reported_to_the_logger(self):
        messages = []
        with mock.patch("builtins.__import__", side_effect=ImportError("no nlpaug")):
            humanize_queries(["a query"], messages.append)
        self.assertTrue(messages)
        self.assertIn("nlpaug", messages[0])

    def test_constructor_failure_is_also_survivable(self):
        """A frozen build whose nlpaug metadata is missing raises on import."""
        queries = ["banana bread"]
        # Raised with a package name, as nlpaug raises it in a frozen build.
        missing = PackageNotFoundError("nlpaug")
        with mock.patch("builtins.__import__", side_effect=missing):
            self.assertEqual(humanize_queries(queries), queries)

    def test_augment_failure_keeps_the_original_query(self):
        class ExplodingAugmenter:
            def augment(self, query):
                raise RuntimeError("augment failed")

        fake_nac = mock.Mock()
        fake_nac.KeyboardAug.return_value = ExplodingAugmenter()
        with mock.patch.dict("sys.modules", {"nlpaug.augmenter.char": fake_nac}):
            with mock.patch("src.utils.random.random", return_value=0.0):
                result = humanize_queries(["keep me"])
        self.assertEqual(result, ["keep me"])

    def test_empty_input_is_fine(self):
        self.assertEqual(humanize_queries([]), [])


class LlmVarietyTests(unittest.TestCase):
    """Issue #159: batches must not collapse onto one repeated topic."""

    def test_avoid_list_reaches_the_prompt(self):
        prompts = []

        def caller(prompt, model, key, max_tokens, logger):
            prompts.append(prompt)
            return '["a new topic"]'

        with mock.patch.dict(llm._DISPATCH, {"openai": caller}):
            llm.generate_queries(
                1,
                "en-US",
                provider="openai",
                api_key="k",
                avoid=["old topic one", "old topic two"],
            )

        self.assertIn("old topic one", prompts[0])
        self.assertIn("do not repeat", prompts[0])

    def test_previously_used_queries_are_filtered_out(self):
        with mock.patch.dict(
            llm._DISPATCH,
            {"openai": lambda p, m, k, t, lg: '["repeat", "fresh"]'},
        ):
            result = llm.generate_queries(
                2, "en-US", provider="openai", api_key="k", avoid={"repeat"}
            )
        self.assertEqual(result, ["fresh"])

    def test_filtering_is_case_insensitive(self):
        with mock.patch.dict(
            llm._DISPATCH,
            {"openai": lambda p, m, k, t, lg: '["Weather Paris", "fresh topic"]'},
        ):
            result = llm.generate_queries(
                2, "en-US", provider="openai", api_key="k", avoid={"weather paris"}
            )
        self.assertEqual(result, ["fresh topic"])

    def test_accepted_queries_are_collected_for_later_batches(self):
        used = set()
        with mock.patch.dict(
            llm._DISPATCH,
            {"openai": lambda p, m, k, t, lg: '["one", "two"]'},
        ):
            llm.generate_queries(2, "en-US", provider="openai", api_key="k", used=used)
        self.assertEqual(used, {"one", "two"})

    def test_no_avoid_list_leaves_the_prompt_alone(self):
        prompt = llm._build_prompt(5, "en-US")
        self.assertNotIn("do not repeat", prompt)


class RunQueryPlanningTests(unittest.TestCase):
    """Issue #159: the advanced scheduler plans the run once, in real batches."""

    def _api(self):
        import src.api as api_module

        api = api_module.AutoRewarderAPI.__new__(api_module.AutoRewarderAPI)
        api.log = lambda message: None
        api._query_plan = []
        api._run_queries = set()
        api._run_planned_total = 0
        # Deliberately NOT setting _QUERY_PLAN_BATCH here: it must come from the
        # class. Setting it on the instance once masked a real crash where the
        # planning code read a constant the instance never had.
        settings = mock.Mock()
        settings.get_llm_config.return_value = {
            "use_llm_queries": True,
            "llm_api_key": "key",
            "llm_provider": "openai",
            "llm_model": "",
            "llm_base_url": "",
        }
        settings.get_settings.return_value = {}
        api.global_settings = settings
        return api

    def test_plan_asks_for_batches_larger_than_one(self):
        import src.api as api_module

        api = self._api()
        api._run_planned_total = 30
        asked = []

        def fake_generate(count, locale, **kwargs):
            asked.append(count)
            return [f"q{len(asked)}-{i}" for i in range(count)]

        with mock.patch.object(api_module, "llm") as fake_llm, mock.patch.object(
            api_module, "resolve_search_locale", return_value="en-US"
        ):
            fake_llm.generate_queries.side_effect = fake_generate
            api._plan_run_queries()

        self.assertTrue(asked, "the planner must ask the LLM for queries")
        self.assertTrue(
            all(count > 1 for count in asked),
            f"batches of 1 are the bug; asked for {asked}",
        )

    def test_plan_covers_the_requested_total_without_repeats(self):
        import src.api as api_module

        api = self._api()
        api._run_planned_total = 25
        counter = {"n": 0}

        def fake_generate(count, locale, **kwargs):
            counter["n"] += 1
            return [f"batch{counter['n']}-{i}" for i in range(count)]

        with mock.patch.object(api_module, "llm") as fake_llm, mock.patch.object(
            api_module, "resolve_search_locale", return_value="en-US"
        ):
            fake_llm.generate_queries.side_effect = fake_generate
            api._plan_run_queries()

        self.assertEqual(len(api._query_plan), 25)
        self.assertEqual(len(set(api._query_plan)), 25)

    def test_batches_are_served_from_the_plan(self):
        api = self._api()
        api._query_plan = ["a", "b", "c", "d"]

        self.assertEqual(api._next_planned_queries(1), ["a"])
        self.assertEqual(api._next_planned_queries(2), ["b", "c"])
        self.assertEqual(api._next_planned_queries(5), ["d"])
        self.assertEqual(api._next_planned_queries(1), [])

    def test_no_plan_leaves_the_existing_path_untouched(self):
        api = self._api()
        self.assertEqual(api._next_planned_queries(3), [])

    def test_the_planner_reads_its_batch_size_from_the_class(self):
        """
        Regression: _plan_run_queries used `self._QUERY_PLAN_BATCH`, which was
        only a module constant, so advanced scheduling with an LLM key raised
        AttributeError on a real object. A test that set the attribute by hand
        hid it. This asserts the class provides it on an instance built without
        __init__.
        """
        import src.api as api_module

        api = self._api()
        self.assertEqual(
            api._QUERY_PLAN_BATCH,
            api_module._QUERY_PLAN_BATCH,
            "the instance must expose the class constant the planner reads",
        )
        self.assertGreater(api._QUERY_PLAN_BATCH, 1, "batches of 1 are the #159 bug")

    def test_planning_with_a_key_does_not_raise(self):
        """The whole path, with nothing patched onto the instance."""
        import src.api as api_module

        api = self._api()
        api._run_planned_total = 20

        def fake_generate(count, locale, **kwargs):
            return [f"q{count}-{i}" for i in range(count)]

        with mock.patch.object(api_module, "llm") as fake_llm, mock.patch.object(
            api_module, "resolve_search_locale", return_value="en-US"
        ):
            fake_llm.generate_queries.side_effect = fake_generate
            api._plan_run_queries()  # must not raise AttributeError

        self.assertEqual(len(api._query_plan), 20)

    def test_planning_is_skipped_without_llm(self):
        import src.api as api_module

        api = self._api()
        api._run_planned_total = 20
        api.global_settings.get_llm_config.return_value = {"use_llm_queries": False}

        with mock.patch.object(api_module, "llm") as fake_llm:
            api._plan_run_queries()
            fake_llm.generate_queries.assert_not_called()

        self.assertEqual(api._query_plan, [])


class BalanceDeltaTests(unittest.TestCase):
    """Issue #161: a redemption must not display as negative earned points."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.stats = StatsManager(os.path.join(self.directory.name, "stats.json"))

    def test_redeeming_points_does_not_report_negative_earnings(self):
        self.stats.record_session(pc_searches=10, balance=5000)
        # The user redeems a gift card, so the real balance drops.
        self.stats.record_session(pc_searches=5, balance=4200)

        last = self.stats.get_stats()["last_session"]
        self.assertIsNone(
            last["points_delta"],
            "a spend must not be shown as a negative points delta",
        )
        self.assertEqual(last["balance_change"], -800)
        self.assertEqual(self.stats.get_stats()["balance"]["current"], 4200)

    def test_earning_again_after_a_redemption_still_works(self):
        self.stats.record_session(pc_searches=10, balance=5000)
        self.stats.record_session(pc_searches=5, balance=4200)
        self.stats.record_session(pc_searches=5, balance=4300)

        self.assertEqual(self.stats.get_stats()["last_session"]["points_delta"], 100)

    def test_a_normal_gain_is_still_reported(self):
        self.stats.record_session(pc_searches=10, balance=5000)
        self.stats.record_session(pc_searches=10, balance=5090)

        last = self.stats.get_stats()["last_session"]
        self.assertEqual(last["points_delta"], 90)
        self.assertEqual(last["balance_change"], 90)

    def test_first_ever_reading_has_no_delta(self):
        self.stats.record_session(pc_searches=10, balance=5000)

        last = self.stats.get_stats()["last_session"]
        self.assertIsNone(last["points_delta"])
        self.assertIsNone(last["balance_change"])


if __name__ == "__main__":
    unittest.main()
