"""Report-based earning, and the evidence that decides "credited".

The point of this module is that a click is not evidence. These tests hold the
two halves of that apart:

  * reading the offer list — every shape the flyout uses for its groups, the
    per-offer fields, and the two ways it says "you are not signed in";
  * reporting — the request body, what a response may and may not be read as,
    and the loop that stops when an offer does not move.

The fake page models the rule that matters: a report is credited only when the
offer's own progress advances, and a report to an offer the account has already
finished advances nothing — which is exactly the case that used to be reported
as earned.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.dailytasks.rewards_api import (  # noqa: E402
    RewardsApiEarner,
    available_points,
    count_verified,
    credit_moved,
    extract_view_model,
    is_reportable,
    looks_signed_out,
    normalise_offer,
    offers_from_view_model,
    parse_view_model,
    report_body,
    response_accepted,
    response_balance,
    todays_offers,
)


def _promotion(offer_id, progress=0, maximum=1, **extra):
    record = {
        "offerId": offer_id,
        "hash": extra.pop("hash", "hash-" + offer_id),
        "title": extra.pop("title", offer_id),
        "pointProgress": progress,
        "pointProgressMax": maximum,
        "complete": extra.pop("complete", False),
    }
    record.update(extra)
    return record


def _model(**groups):
    return {
        "channel": "BingFlyout",
        "partnerId": "BingRewards",
        "userId": "user-1",
        "isRewardsUser": True,
        "flyoutResult": {
            "userStatus": {"availablePoints": groups.pop("balance", 100)},
            **groups,
        },
    }


class ViewModelParsingTests(unittest.TestCase):
    def test_the_assignment_object_is_extracted(self):
        html = (
            "<script>window.flyoutViewModel = "
            '{"userId": "abc", "flyoutResult": {"morePromotions": []}};'
            "</script>"
        )
        model = extract_view_model(html)
        self.assertEqual(model["userId"], "abc")

    def test_braces_inside_strings_do_not_end_the_object(self):
        html = (
            "window.flyoutViewModel = "
            '{"title": "a { brace } inside", "flyoutResult": {}};'
        )
        model = extract_view_model(html)
        self.assertEqual(model["title"], "a { brace } inside")

    def test_escaped_quotes_do_not_end_the_object(self):
        html = (
            "window.flyoutViewModel = "
            '{"title": "he said \\"hi\\"", "flyoutResult": {}};'
        )
        model = extract_view_model(html)
        self.assertEqual(model["title"], 'he said "hi"')

    def test_a_page_without_a_model_reads_as_none(self):
        self.assertIsNone(extract_view_model("<html><body>Sign in</body></html>"))
        self.assertIsNone(extract_view_model(""))
        self.assertIsNone(extract_view_model(None))

    def test_a_truncated_object_reads_as_none(self):
        self.assertIsNone(extract_view_model('window.flyoutViewModel = {"a": {"b": 1}'))

    def test_the_pages_json_string_is_parsed(self):
        self.assertEqual(parse_view_model('{"a": 1}'), {"a": 1})
        self.assertIsNone(parse_view_model("not json"))
        self.assertIsNone(parse_view_model(""))


class OfferReadingTests(unittest.TestCase):
    def test_offers_are_read_from_a_list_group(self):
        model = _model(morePromotions=[_promotion("offer-a"), _promotion("offer-b")])
        offers = offers_from_view_model(model)
        self.assertEqual({o["offer_id"] for o in offers}, {"offer-a", "offer-b"})

    def test_the_daily_set_is_read_when_grouped_by_date(self):
        model = _model(
            dailySetPromotions={
                "11/17/2026": [_promotion("daily-a")],
                "11/18/2026": [_promotion("daily-b", locked=True)],
            }
        )
        offers = offers_from_view_model(model)
        self.assertEqual({o["offer_id"] for o in offers}, {"daily-a", "daily-b"})

    def test_a_single_promotion_group_is_read(self):
        model = _model(dailyCheckInPromotion=_promotion("check-in"))
        offers = offers_from_view_model(model)
        self.assertEqual([o["offer_id"] for o in offers], ["check-in"])

    def test_punchcards_and_their_children_are_read(self):
        model = _model(
            punchCards=[
                {
                    "parentPromotion": _promotion("quest-1", maximum=50),
                    "childPromotions": [
                        _promotion("quest-1-child", maximum=10),
                    ],
                }
            ]
        )
        # The children carry their own hashes; both are addressable offers.
        offers = {o["offer_id"]: o for o in offers_from_view_model(model)}
        self.assertIn("quest-1-child", offers)

    def test_an_offer_in_two_groups_is_kept_once_at_its_best_progress(self):
        model = _model(
            dailySetPromotions={"11/17/2026": [_promotion("offer-a", progress=0)]},
            morePromotions=[_promotion("offer-a", progress=1, maximum=1)],
        )
        offers = offers_from_view_model(model)
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["progress"], 1)

    def test_an_offer_keeps_its_group_when_it_appears_twice(self):
        """
        One offer, one owner. If its `source` could flip between reads, the
        Daily Set would stop counting it and the earn-page pass would try to
        report it again.
        """
        model = _model(
            dailySetPromotions={"11/17/2026": [_promotion("offer-a", progress=0)]},
            morePromotions=[_promotion("offer-a", progress=0)],
        )
        first = offers_from_view_model(model)
        model["flyoutResult"]["morePromotions"] = [
            _promotion("offer-a", progress=1, maximum=1)
        ]
        second = offers_from_view_model(model)
        self.assertEqual(first[0]["source"], "dailySetPromotions")
        self.assertEqual(second[0]["source"], "dailySetPromotions")
        self.assertEqual(second[0]["progress"], 1)

    def test_an_offer_without_an_id_is_ignored(self):
        model = _model(morePromotions=[{"hash": "x", "title": "no id"}])
        self.assertEqual(offers_from_view_model(model), [])

    def test_progress_and_maximum_are_read_as_numbers(self):
        model = _model(
            morePromotions=[_promotion("offer-a", progress="1", maximum="3")]
        )
        offer = offers_from_view_model(model)[0]
        self.assertEqual((offer["progress"], offer["maximum"]), (1, 3))

    def test_the_balance_is_read_from_the_model(self):
        self.assertEqual(available_points(_model(balance=1234)), 1234)
        self.assertIsNone(available_points({}))


class SignedOutTests(unittest.TestCase):
    def test_an_anonymous_flyout_is_signed_out(self):
        # The page renders, offers render, and every report credits nobody.
        anonymous = _model()
        anonymous["userId"] = ""
        self.assertTrue(looks_signed_out(anonymous))

    def test_the_rewards_user_flag_is_believed(self):
        anonymous = _model()
        anonymous["isRewardsUser"] = False
        self.assertTrue(looks_signed_out(anonymous))

    def test_a_signed_in_flyout_is_not(self):
        self.assertFalse(looks_signed_out(_model()))


class ReportableTests(unittest.TestCase):
    def test_an_incomplete_offer_with_a_hash_is_reportable(self):
        self.assertTrue(is_reportable(_promotion("a")))

    def test_an_offer_without_a_hash_is_not(self):
        self.assertFalse(is_reportable(_promotion("a", hash=None)))

    def test_a_complete_offer_is_not(self):
        self.assertFalse(is_reportable(_promotion("a", complete=True)))

    def test_a_locked_offer_is_not(self):
        self.assertFalse(
            is_reportable({**_promotion("a"), "exclusiveLockedFeatureStatus": "locked"})
        )

    def test_an_offer_at_its_maximum_is_not(self):
        self.assertFalse(is_reportable(_promotion("a", progress=3, maximum=3)))

    def test_a_promotion_that_sells_something_is_not(self):
        for title in (
            "Refer a friend and earn",
            "Install the Bing extension",
            "Microsoft 365",
            "Enter to win a prize",
        ):
            self.assertFalse(is_reportable(_promotion("a", title=title)), title)


class ReportBodyTests(unittest.TestCase):
    def test_the_body_matches_what_the_site_sends(self):
        body = report_body(_promotion("offer-a", activityType=11), user_id="user-1")
        self.assertEqual(body["OfferId"], "offer-a")
        self.assertEqual(body["AuthKey"], "hash-offer-a")
        self.assertEqual(body["ActivityType"], 11)
        self.assertEqual(body["Channel"], "BingFlyout")
        self.assertEqual(body["PartnerId"], "BingRewards")
        self.assertEqual(body["UserId"], "user-1")
        self.assertEqual(body["ActivityCount"], 1)

    def test_a_missing_activity_type_falls_back_to_a_redirect(self):
        body = report_body(_promotion("offer-a"), user_id="u")
        self.assertEqual(body["ActivityType"], "bingredirect")


class ResponseReadingTests(unittest.TestCase):
    def test_an_accepted_report_is_recognised(self):
        self.assertTrue(response_accepted({"status": 200, "body": '{"ErrorCode": 0}'}))

    def test_a_body_without_an_error_code_is_accepted(self):
        self.assertTrue(response_accepted({"status": 200, "body": '{"Balance": 10}'}))

    def test_an_error_detail_is_a_rejection(self):
        self.assertFalse(
            response_accepted(
                {
                    "status": 200,
                    "body": '{"ErrorDetail": {"ErrorCode": 7}, "ErrorCode": 0}',
                }
            )
        )

    def test_a_transport_failure_is_not_an_acceptance(self):
        self.assertFalse(response_accepted(None))
        self.assertFalse(response_accepted({"status": 0, "error": "offline"}))
        self.assertFalse(response_accepted({"status": 500, "body": "{}"}))

    def test_the_balance_is_read_when_present(self):
        self.assertEqual(
            response_balance({"status": 200, "body": '{"Balance": "1,240"}'}), 1240
        )
        self.assertIsNone(response_balance({"status": 200, "body": "{}"}))


class CreditEvidenceTests(unittest.TestCase):
    def test_progress_going_up_is_credit(self):
        before = _promotion("a", progress=0, maximum=1)
        after = _promotion("a", progress=1, maximum=1)
        self.assertTrue(credit_moved(before, after))

    def test_flipping_to_complete_is_credit(self):
        self.assertTrue(credit_moved(_promotion("a"), _promotion("a", complete=True)))

    def test_an_unchanged_offer_is_not_credit(self):
        """The exact case the old passes reported as done."""
        self.assertFalse(credit_moved(_promotion("a"), _promotion("a")))

    def test_a_missing_counter_is_not_credit(self):
        self.assertFalse(
            credit_moved(
                {"offer_id": "a", "progress": None},
                {"offer_id": "a", "progress": 1, "complete": False},
            )
        )


class TodayAndCountTests(unittest.TestCase):
    def test_the_daily_set_keeps_only_todays_date(self):
        model = _model(
            dailySetPromotions={
                "11/17/2026": [_promotion("today-a")],
                "11/18/2026": [_promotion("tomorrow")],
            }
        )
        offers = todays_offers(offers_from_view_model(model), "dailySetPromotions")
        self.assertEqual([o["offer_id"] for o in offers], ["today-a"])

    def test_an_undated_offer_in_the_daily_set_is_kept(self):
        model = _model(
            dailySetPromotions={
                "11/17/2026": [_promotion("today-a")],
                "11/18/2026": [_promotion("tomorrow")],
            },
            morePromotions=[_promotion("undated")],
        )
        offers = {o["offer_id"]: o for o in offers_from_view_model(model)}
        offers["undated"]["source"] = "dailySetPromotions"
        selected = todays_offers(list(offers.values()), "dailySetPromotions")
        self.assertIn("undated", {o["offer_id"] for o in selected})

    def test_other_groups_are_returned_untouched(self):
        model = _model(
            dailySetPromotions={"11/17/2026": [_promotion("daily")]},
            morePromotions=[_promotion("more")],
        )
        offers = todays_offers(offers_from_view_model(model), "morePromotions")
        self.assertEqual([o["offer_id"] for o in offers], ["more"])

    def test_verification_counts_by_identity_not_by_what_is_left(self):
        before = [
            {"offer_id": "a", "complete": False},
            {"offer_id": "b", "complete": False},
        ]
        # Both complete: the list may have dropped them, which is why the
        # original cards are what gets looked up.
        after = [
            {"offer_id": "a", "complete": True},
            {"offer_id": "b", "complete": True},
        ]
        self.assertEqual(
            count_verified(before, after), {"total": 2, "final": 2, "newly": 2}
        )

    def test_a_partial_after_read_proves_nothing(self):
        before = [
            {"offer_id": "a", "complete": False},
            {"offer_id": "b", "complete": False},
        ]
        self.assertIsNone(count_verified(before, [{"offer_id": "a", "complete": True}]))

    def test_no_before_read_proves_nothing(self):
        self.assertIsNone(count_verified([], [{"offer_id": "a", "complete": True}]))


class _FakeEarner(RewardsApiEarner):
    """
    An earner whose page credits what the fake says it credits.

    Each report is answered as accepted, and the model re-read only advances an
    offer when `creditable` says so — so a pass can be seen reporting without
    earning, and earning when the page agrees.
    """

    def __init__(self, offers, creditable=True, balance=None, step=None):
        self.offers_state = [normalise_offer(offer) for offer in offers]
        self.creditable = creditable
        # How much one report is worth, in progress points. None means "the
        # whole offer", which is how a Daily Set card behaves: one report
        # credits all ten of its points at once. A step of 1 models Read to
        # Earn, where each read adds one until the card is finished.
        self.step = step
        self.model_balance = balance
        self.reports = []
        self.refreshes = 0
        super().__init__(driver=None, logger=None, pacing=False)

    def offers(self):
        return self.offers_state

    def offer(self, offer_id):
        for offer in self.offers_state:
            if offer["offer_id"] == offer_id:
                return offer
        return None

    def balance(self):
        return self.model_balance

    def refresh(self):
        self.refreshes += 1
        return {}

    def report(self, offer, stop_event=None):
        self.reports.append(offer["offer_id"])
        if self.creditable:
            for index, current in enumerate(self.offers_state):
                if current["offer_id"] != offer["offer_id"]:
                    continue
                progress = current.get("progress") or 0
                maximum = current.get("maximum") or 0
                step = self.step if self.step is not None else (maximum or 1)
                progress = min(maximum, progress + step) if maximum else progress + step
                self.offers_state[index] = {
                    **current,
                    "progress": progress,
                    "complete": bool(maximum) and progress >= maximum,
                }
        return {"status": 200, "body": '{"ErrorCode": 0}'}


class EarningTests(unittest.TestCase):
    def test_an_offer_that_credits_is_counted(self):
        earner = _FakeEarner([_promotion("a", progress=0, maximum=1)])
        result = earner.earn(earner.offers())
        self.assertEqual(result["verified"], 1)
        self.assertEqual(result["reports"], 1)

    def test_an_offer_that_does_not_move_is_not_counted(self):
        """The failure this module exists for: accepted report, no points."""
        earner = _FakeEarner([_promotion("a")], creditable=False)
        result = earner.earn(earner.offers())
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["verified"], 0)

    def test_a_points_valued_offer_is_reported_once(self):
        """
        `pointProgressMax` is the offer's points, not a step count: a Daily Set
        card reads 0/10 because it is worth ten points in one report. Reporting
        it ten times would be nine pointless requests.
        """
        earner = _FakeEarner([_promotion("card", progress=0, maximum=10)])
        result = earner.earn(earner.offers())
        self.assertEqual(result["verified"], 1)
        self.assertEqual(len(earner.reports), 1)

    def test_a_multi_step_offer_is_reported_until_it_completes(self):
        earner = _FakeEarner([_promotion("read", progress=0, maximum=3)], step=1)
        result = earner.earn(earner.offers())
        self.assertEqual(result["verified"], 1)
        self.assertEqual(len(earner.reports), 3)
        self.assertTrue(earner.offer("read")["complete"])

    def test_the_step_count_is_capped(self):
        earner = _FakeEarner([_promotion("read", progress=0, maximum=999)], step=1)
        earner.earn(earner.offers(), max_reports_per_offer=4)
        self.assertEqual(len(earner.reports), 4)

    def test_a_locked_or_complete_offer_is_left_alone(self):
        earner = _FakeEarner(
            [
                _promotion("done", progress=1, maximum=1, complete=True),
                {**_promotion("locked"), "exclusiveLockedFeatureStatus": "locked"},
            ]
        )
        result = earner.earn(earner.offers())
        self.assertEqual(result["attempted"], 0)
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(earner.reports, [])

    def test_a_stop_request_sends_nothing_more(self):
        import threading

        earner = _FakeEarner([_promotion("a"), _promotion("b")])
        stop = threading.Event()
        stop.set()
        result = earner.earn(earner.offers(), stop_event=stop)
        self.assertEqual(earner.reports, [])
        self.assertEqual(result["attempted"], 0)


if __name__ == "__main__":
    unittest.main()
