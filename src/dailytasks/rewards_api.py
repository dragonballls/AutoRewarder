"""
Earn through the Rewards activity API, and verify the credit.

Every pass in this app used to work the same way: find a card, click it like a
person, and assume the visit was credited. On the new dashboard that assumption
is wrong, and it is why these steps earn nothing:

  * credit is recorded server-side by an activity report, not by the page you
    land on — so an article opened for fifteen seconds, a card opened in a new
    tab and a quest task clicked through all return "done" to us and nothing to
    Rewards;
  * the dashboard's own data already says which offers are reportable (each
    carries a `hash`, an activity type and a `pointProgress`/`pointProgressMax`
    pair), so "did it credit?" can be answered from the page instead of guessed;
  * a click that lands on a card we mis-identified still counts as success,
    which is how Daily check-in kept reporting *clicked, but the card did not
    confirm it* — the control it found was a label containing "check in", not
    the check-in itself.

This module does what the Rewards page itself does, against the same endpoints:

  1. read the flyout view model — the JSON the site renders at
     ``bing.com/rewards/panelflyout``, which lists every offer with its
     `offerId`, `hash`, activity type, progress and completion flag;
  2. POST an activity report for each offer that is still incomplete;
  3. re-read the model and count the offer as earned only when its own progress
     moved, or the balance rose.

Nothing is trusted from the response alone. An unauthenticated POST to the
report endpoint answers ``ErrorCode: 0`` and a full user-status body, so an
acceptance proves the request was well formed and never that points were added.
The re-read is the only evidence.

The session is the browser's: reads and writes happen inside the signed-in page,
so the app needs no token handling of its own.
"""

import json
import random
import re
import time

from selenium.common.exceptions import WebDriverException

FLYOUT_URL = (
    "https://www.bing.com/rewards/panelflyout"
    "?channel=bingflyout&partnerId=BingRewards&isDarkMode=0"
    "&ru=https%3A%2F%2Fwww.bing.com%2F"
)
REPORT_URL = "https://www.bing.com/msrewards/api/v1/reportactivity"

# How many individual reports one offer may take. Read to Earn is a handful of
# small reads and a punchcard child is a single report; the cap guards against an
# offer that never advances, it is not a target.
MAX_REPORTS_PER_OFFER = 20

# Groups of promotions in the view model. Some are lists, some a dict keyed by
# date (the Daily Set is grouped by day), some a single promotion.
_OFFER_GROUPS = (
    "dailySetPromotions",
    "morePromotions",
    "morePromotionsWithoutPromotionalItems",
    "punchCards",
    "exploreOnBingPromotions",
    "highValueActionPromotions",
    "edgeHighValueActionPromotions",
    "streakBonusPromotions",
    "onboardingChecklistPromotions",
)

_SINGLE_GROUPS = (
    "dailyCheckInPromotion",
    "streakPromotion",
    "searchAndEarnPromotion",
    "pointClaimCardPromotion",
    "searchStreakPromotion",
    "bingTrialStreakPromotion",
    "oneTimeStreakPromotion",
    "dailySurprisePromotion",
    "goalCardPromotion",
)

# Promotions that exist to sell something or invite a friend: they carry a hash
# but award no browsing points, and reporting them is noise.
_SKIP_TITLE_RE = re.compile(
    r"refer\s+a\s+friend|invite\s+friends?|\bcash\s*back\b|"
    r"install\s+(?:the\s+)?(?:bing|edge)\s+extension|"
    r"get\s+the\s+(?:bing|edge)\s+app|"
    r"microsoft\s+365|xbox\s+game\s+pass|sweepstake|enter\s+to\s+win",
    re.IGNORECASE,
)

# The view model is a JS assignment followed by a JSON object; find the object
# and hand back its text so Python parses it (JSON.parse is stricter than JS
# evaluation, which keeps this from running page code).
_SLICE_START_RE = re.compile(r"window\.flyoutViewModel\s*=\s*")

# Ask the page for the model it already holds. A string keeps the conversion in
# Python, where it can be tested.
_READ_VIEW_MODEL_JS = r"""
try {
  var vm = window.flyoutViewModel;
  if (!vm) return null;
  return JSON.stringify(vm);
} catch (e) { return null; }
"""

# Re-read the flyout without leaving the page. This is what makes verification
# possible: `window.flyoutViewModel` is the server-rendered object and never
# changes, so only a fresh document can show that a report was credited.
_REFRESH_JS = r"""
var done = arguments[arguments.length - 1];
try {
  fetch(arguments[0], { credentials: 'include', headers: { 'Accept': 'text/html' } })
    .then(function (r) { return r.text(); })
    .then(function (t) { done({ status: 200, body: t }); })
    .catch(function (e) { done({ status: 0, error: String(e).slice(0, 120) }); });
} catch (e) { done({ status: 0, error: String(e).slice(0, 120) }); }
"""

# Post one activity report from inside the signed-in page, so it carries the
# browser's cookies. The body shape is the site's own, and the request is sent
# from the flyout itself: that makes it same-origin, and the browser supplies
# the Origin and Referer headers the endpoint expects. (Setting them by hand
# would be silently dropped — they are forbidden header names.)
_REPORT_JS = r"""
var done = arguments[arguments.length - 1];
try {
  fetch(arguments[0], {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      'Accept': '*/*'
    },
    body: arguments[1]
  })
    .then(function (r) {
      return r.text().then(function (t) { done({ status: r.status, body: t }); });
    })
    .catch(function (e) { done({ status: 0, error: String(e).slice(0, 120) }); });
} catch (e) { done({ status: 0, error: String(e).slice(0, 120) }); }
"""


def extract_view_model(html):
    """
    Pull the flyout view model out of a rendered flyout document.

    The page assigns it as ``window.flyoutViewModel = {...}``. The object is
    found by a balanced-brace scan that respects string literals: scraping the
    surrounding markup would break on the next layout change, and a greedy match
    would run to the end of the file.

    Args:
        html (str): the flyout page's HTML.

    Returns:
        dict | None: the parsed view model, or None when the page has none.
    """
    if not isinstance(html, str) or not html:
        return None

    marker = _SLICE_START_RE.search(html)
    if marker is None:
        return None

    start = html.find("{", marker.end())
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(html)):
        char = html[index]
        if escaped:
            escaped = False
            continue
        if in_string and char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    slice_end = index + 1
                    parsed = json.loads(html[start:slice_end])
                except ValueError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def parse_view_model(raw):
    """Turn the page's JSON string into a dict, or None when it is unusable."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _as_int(value):
    """A whole number from a JSON field, or None. Accepts "1,234" and 12."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        digits = re.sub(r"[^0-9]", "", value)
        if digits:
            return int(digits)
    return None


def _truthy_complete(promotion):
    """Whether a promotion says it is finished, whichever field it uses."""
    for field in ("complete", "isCompleted", "isComplete"):
        if promotion.get(field) is True:
            return True
    return False


def _locked(promotion):
    """Whether a promotion is locked (a punchcard step before its day)."""
    status = str(promotion.get("exclusiveLockedFeatureStatus") or "").lower()
    if status in ("locked", "disabled"):
        return True
    for field in ("isLocked", "isDisabled", "locked"):
        if promotion.get(field) is True:
            return True
    return False


def _flatten(group):
    """
    Every promotion inside a group, with its role and date label.

    Returns a list of ``(promotion, role, date_label)`` triples. A group is a
    list, a dict of lists (the Daily Set is keyed by date), or one promotion. A
    punchcard wraps a parent offer and its steps: the parent carries the quest's
    total, and each child is the single step reportable today.
    """
    if isinstance(group, list):
        flat = []
        for item in group:
            flat.extend(_flatten(item))
        return flat
    if isinstance(group, dict):
        if "childPromotions" in group or "parentPromotion" in group:
            inner = []
            parent = group.get("parentPromotion")
            if isinstance(parent, dict):
                inner.append((parent, "parent", None))
            children = group.get("childPromotions")
            if isinstance(children, list):
                inner.extend(
                    (item, "child", None) for item in children if isinstance(item, dict)
                )
            return inner
        # A single promotion is a dict of fields (it has an offerId).
        if "offerId" in group:
            return [(group, None, None)]
        flat = []
        for key, value in group.items():
            if isinstance(value, list):
                flat.extend(
                    (item, None, str(key)) for item in value if isinstance(item, dict)
                )
            elif isinstance(value, dict) and "offerId" in value:
                flat.append((value, None, str(key)))
        return flat
    return []


def _date_key(label):
    """Parse a "MM/DD/YYYY" group label into a comparable (Y, M, D) tuple."""
    parts = str(label or "").split("/")
    if len(parts) != 3:
        return None
    try:
        month, day, year = (int(part) for part in parts)
    except ValueError:
        return None
    return (year, month, day)


def _normalise(promotion, source, role=None, date_label=None):
    """One promotion as the flat record every caller works with."""
    offer_id = promotion.get("offerId") or promotion.get("offerid")
    if not isinstance(offer_id, str) or not offer_id.strip():
        return None

    title = promotion.get("title") or promotion.get("name") or ""
    if not isinstance(title, str):
        title = ""

    progress = _as_int(promotion.get("pointProgress"))
    maximum = _as_int(promotion.get("pointProgressMax"))
    if maximum is None:
        maximum = _as_int(promotion.get("points"))

    activity_type = promotion.get("activityType")
    if isinstance(activity_type, bool) or not isinstance(activity_type, (str, int)):
        activity_type = None

    subtype = promotion.get("promotionSubtype")
    if not isinstance(subtype, str):
        subtype = ""

    return {
        "offer_id": offer_id.strip(),
        "hash": (
            promotion.get("hash") if isinstance(promotion.get("hash"), str) else None
        ),
        "title": title.strip(),
        "source": source,
        "role": role,
        "date": date_label,
        "activity_type": activity_type,
        "subtype": subtype,
        "progress": progress,
        "maximum": maximum,
        "complete": _truthy_complete(promotion),
        "locked": _locked(promotion),
        "destination": promotion.get("destinationUrl") or "",
    }


def normalise_offer(record, source="offer"):
    """
    One offer as the flat record every caller works with.

    Accepts a record already in that shape (it has `offer_id`) as well as a raw
    promotion straight out of the view model, so a caller can pass either.
    Returns None when there is nothing addressable in it.
    """
    if not isinstance(record, dict):
        return None
    if record.get("offer_id"):
        return record
    return _normalise(record, source)


def offers_from_view_model(view_model):
    """
    Every offer the view model holds, de-duplicated by id.

    The same offer can appear in more than one group. The record with the most
    progress wins, so a stale copy elsewhere cannot re-trigger a finished offer.
    """
    if not isinstance(view_model, dict):
        return []

    result = view_model.get("flyoutResult")
    if not isinstance(result, dict):
        result = view_model

    by_id = {}

    def keep(record):
        previous = by_id.get(record["offer_id"])
        if previous is None:
            by_id[record["offer_id"]] = record
            return
        # The same offer can appear in several groups (the Daily Set is also
        # listed among the more promotions). It is one offer, so it gets one
        # record: the group that saw it first owns it, and the most informative
        # progress wins. Without the first-wins rule an offer's `source` could
        # flip between reads, moving it out of the Daily Set's count mid-run.
        merged = dict(previous)
        if record["progress"] is not None and (
            previous["progress"] is None or record["progress"] > previous["progress"]
        ):
            merged["progress"] = record["progress"]
            merged["maximum"] = record["maximum"] or previous["maximum"]
        merged["complete"] = previous["complete"] or record["complete"]
        merged["locked"] = previous["locked"] or record["locked"]
        if not merged["hash"] and record["hash"]:
            merged["hash"] = record["hash"]
        by_id[record["offer_id"]] = merged

    for group_name in _OFFER_GROUPS:
        for promotion, role, date_label in _flatten(result.get(group_name)):
            record = _normalise(promotion, group_name, role, date_label)
            if record is not None:
                keep(record)

    for group_name in _SINGLE_GROUPS:
        group = result.get(group_name)
        if not isinstance(group, dict):
            continue
        record = _normalise(group, group_name)
        if record is not None:
            keep(record)

    return list(by_id.values())


def todays_offers(offers, source):
    """
    The offers in one group that belong to today.

    The Daily Set arrives grouped by date and includes the next few days, all
    of them incomplete until they unlock. Past days are never present, so the
    smallest date in the group is today; undated offers in the group (a referral
    banner, say) belong to today's set as well. Offers from any other group are
    returned untouched — only the Daily Set is date-grouped.
    """
    selected = [offer for offer in offers if offer.get("source") == source]
    if not selected:
        return []

    dated = [
        (_date_key(offer.get("date")), offer)
        for offer in selected
        if _date_key(offer.get("date")) is not None
    ]
    if not dated:
        return selected

    today = min(key for key, _ in dated)
    return [offer for key, offer in dated if key == today] + [
        offer for offer in selected if _date_key(offer.get("date")) is None
    ]


def count_verified(before_offers, after_offers):
    """
    How many of `before_offers` Rewards' own data says are finished now.

    Counted by identity, never by how many cards are left: a finished offer can
    disappear from the list entirely, which used to read as "nothing left, all
    done" or "one card left, one done" depending on when it was read.

    Args:
        before_offers (list): the records taken before the run.
        after_offers (list): every record read at the end.

    Returns:
        dict: {"total", "final", "newly"}, or None when the after-read does not
            contain every original offer (a partial read proves nothing).
    """
    if not isinstance(before_offers, list) or not before_offers:
        return None
    if not isinstance(after_offers, list) or not after_offers:
        return None

    after_by_id = {}
    for offer in after_offers:
        if isinstance(offer, dict) and offer.get("offer_id"):
            after_by_id[offer["offer_id"]] = offer

    before_by_id = {}
    for offer in before_offers:
        if not isinstance(offer, dict) or not offer.get("offer_id"):
            return None
        if offer["offer_id"] in before_by_id:
            return None
        before_by_id[offer["offer_id"]] = offer

    if any(offer_id not in after_by_id for offer_id in before_by_id):
        return None

    final = sum(1 for offer_id in before_by_id if after_by_id[offer_id].get("complete"))
    newly = sum(
        1
        for offer_id, offer in before_by_id.items()
        if not offer.get("complete") and after_by_id[offer_id].get("complete")
    )
    return {"total": len(before_by_id), "final": final, "newly": newly}


def is_reportable(offer):
    """
    Whether an offer is worth a report right now.

    Needs a live `hash` (Rewards' per-offer token), must not be complete or
    locked, and must not be a promotion that only sells something.
    """
    offer = normalise_offer(offer)
    if offer is None:
        return False
    if not offer.get("hash") or not offer.get("offer_id"):
        return False
    if offer.get("complete") or offer.get("locked"):
        return False
    if offer.get("role") == "parent":
        # A punchcard's parent is a container: its progress counts its steps.
        # Reporting it credits nothing; the steps carry the hashes to report.
        return False
    if _SKIP_TITLE_RE.search(offer.get("title") or ""):
        return False

    progress, maximum = offer.get("progress"), offer.get("maximum")
    if isinstance(progress, int) and isinstance(maximum, int) and maximum > 0:
        return progress < maximum
    # No usable counter: reportable, but it cannot be verified afterwards.
    return True


def report_body(offer, user_id="", channel="BingFlyout", partner_id="BingRewards"):
    """The request body Rewards' own flyout sends for one activity report."""
    offer = normalise_offer(offer)
    if offer is None:
        raise ValueError("An offer with an id is required to report it.")
    activity_type = offer.get("activity_type")
    if isinstance(activity_type, bool) or activity_type in (None, ""):
        activity_type = "bingredirect"
    return {
        "ActivityType": activity_type,
        "ActivitySubType": offer.get("subtype") or "",
        "OfferId": offer["offer_id"],
        "Channel": channel or "BingFlyout",
        "PartnerId": partner_id or "BingRewards",
        "UserId": user_id or "",
        "AuthKey": offer["hash"],
        "ActivityCount": 1,
    }


def _body_of(response):
    """The JSON body of a response, parsed when it arrives as text."""
    if not isinstance(response, dict):
        return None
    body = response.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return None
    return body if isinstance(body, dict) else None


def response_accepted(response):
    """
    Whether a report response is a well-formed acceptance.

    Deliberately *not* the evidence a point was earned: an unauthenticated POST
    answers exactly the same way. It separates "the request worked" from "the
    request was rejected".
    """
    if not isinstance(response, dict):
        return False
    if response.get("status") not in (200, 201):
        return False
    body = _body_of(response)
    if body is None:
        return False
    detail = body.get("ErrorDetail")
    if isinstance(detail, dict) and detail.get("ErrorCode") not in (None, 0):
        return False
    if body.get("ErrorCode") not in (None, 0):
        return False
    return True


def response_balance(response):
    """The balance a report response reports, when it carries one."""
    body = _body_of(response)
    if body is None:
        return None
    return _as_int(body.get("Balance"))


def available_points(view_model):
    """
    The account's available points, straight from the view model.

    The flyout carries the balance in `userStatus.availablePoints` — the number
    the dashboard card shows, but read from the payload instead of a DOM node
    that may not have rendered. That is why the balance scrape kept landing on
    "not found", or on a sign-in page where no balance exists at all.
    """
    if not isinstance(view_model, dict):
        return None
    result = view_model.get("flyoutResult")
    if not isinstance(result, dict):
        result = view_model
    status = result.get("userStatus")
    if not isinstance(status, dict):
        return None
    return _as_int(status.get("availablePoints"))


def looks_signed_out(view_model):
    """
    Whether the flyout says this session is not signed in to Rewards.

    An anonymous flyout still renders — ``userId: ""``, ``isRewardsUser: false``
    — so it looks like a working page while every report credits nobody. Saying
    so is the difference between "your account is finished for the day" and
    "your account is signed out".
    """
    if not isinstance(view_model, dict):
        return False
    if view_model.get("isRewardsUser") is False:
        return True
    user_id = view_model.get("userId")
    return isinstance(user_id, str) and user_id.strip() == ""


def credit_moved(before, after):
    """
    Whether one offer's state says a report was credited.

    Progress going up is the evidence. So is the offer flipping to complete,
    which covers an offer whose counter is absent. Nothing else counts: an
    unchanged progress after an accepted report means Rewards took the request
    and gave nothing, which is exactly the case the old code reported as done.
    """
    before = normalise_offer(before) if before is not None else None
    after = normalise_offer(after)
    if after is None:
        return False
    if after.get("complete"):
        return True

    before_progress = before.get("progress") if before else None
    after_progress = after.get("progress")
    if not isinstance(after_progress, int):
        return False
    if not isinstance(before_progress, int):
        # No starting counter to compare against: a positive value after a
        # report is not proof of this report. Do not claim it.
        return False
    return after_progress > before_progress


class RewardsApiEarner:
    """
    Reads the Rewards offer list and reports activities, on one signed-in tab.

    One instance per run. Every method reports rather than raises: a page that
    changed shape must leave the rest of the run working, and the caller decides
    what to do with a zero.
    """

    def __init__(self, driver, logger=None, pacing=True):
        self.driver = driver
        self.logger = logger
        self._pacing = pacing
        self.view_model = None
        self._user_id = ""
        self.last_report_error = None
        # What the last read said about the session: "signed_in", "signed_out"
        # or "unknown" (the offer list could not be read, which proves nothing).
        self.last_state = "unknown"
        try:
            driver.set_script_timeout(25)
        except Exception:
            pass

    # -- Plumbing -------------------------------------------------------------

    def _log(self, message):
        if self.logger:
            self.logger(message)

    def _pause(self, low, high):
        if self._pacing:
            time.sleep(random.uniform(low, high))

    def _stopped(self, stop_event):
        return stop_event is not None and stop_event.is_set()

    # -- Reading --------------------------------------------------------------

    def load(self, navigate=True, quiet=False):
        """
        Read the flyout view model, navigating to it when needed.

        Args:
            navigate (bool): open the flyout first. Pass False to fetch a fresh
                copy while staying on the current page.
            quiet (bool): keep the log clean. Used by a check whose answer is
                reported by its caller (First Setup), so a signed-out session
                is not also shouted about as a run failure.

        Returns:
            dict | None: the view model, also kept on the instance.
        """
        if navigate:
            try:
                self.driver.get(FLYOUT_URL)
            except WebDriverException as e:
                if not quiet:
                    self._log(f"[WARNING] Could not open the Rewards flyout: {e}")
                return None
            self._pause(1.2, 2.0)

        view_model = self._read_local_model()
        if view_model is None:
            view_model = self.refresh()

        if view_model is None:
            self.last_state = "unknown"
            if not quiet:
                self._log(
                    "[WARNING] Could not read the Rewards offer list. Rewards may "
                    "be mid-update; the page's own cards are still used where they "
                    "can be."
                )
            return None

        if looks_signed_out(view_model):
            self.last_state = "signed_out"
            if not quiet:
                self._log(
                    "[ERROR] Rewards says this browser is not signed in, so no "
                    "activity can be reported for this account. Open the account's "
                    "browser with First Setup, sign in to Microsoft, and close the "
                    "window."
                )
            return None

        self.last_state = "signed_in"
        self._remember(view_model)
        points = available_points(view_model)
        if not quiet:
            if points is not None:
                self._log(f"Rewards activity list read (balance {points:,}).")
            else:
                self._log("Rewards activity list read.")
        return view_model

    def session_state(self, navigate=True):
        """
        Whether this profile is signed in to Rewards.

        Three answers, because two would be a lie: a flyout that says signed-out
        is a fact, while one that could not be read is not evidence either way —
        reporting that as "signed out" would send someone to redo a sign-in that
        was fine.

        Returns:
            str: "signed_in", "signed_out" or "unknown".
        """
        self.load(navigate=navigate, quiet=True)
        return self.last_state

    def _read_local_model(self):
        """The model the open page holds, if any."""
        try:
            return parse_view_model(self.driver.execute_script(_READ_VIEW_MODEL_JS))
        except WebDriverException:
            return None

    def _remember(self, view_model):
        user_id = view_model.get("userId")
        self._user_id = user_id if isinstance(user_id, str) else ""
        self.view_model = view_model

    def refresh(self):
        """
        Fetch a fresh copy of the flyout and read its model.

        This is the verification read: the open page's model is a snapshot from
        when it rendered and never updates, so only a new document can show that
        a report moved an offer or the balance.

        A same-origin `fetch` leaves the page as it is, which matters when the
        caller is mid-run on the flyout. From any other page — the dashboard on
        rewards.bing.com, an article — that fetch is cross-origin and refused, so
        this falls back to navigating there.

        Returns:
            dict | None: the model, also kept on the instance.
        """
        response = None
        try:
            response = self.driver.execute_async_script(_REFRESH_JS, FLYOUT_URL)
        except WebDriverException:
            response = None

        view_model = None
        if isinstance(response, dict) and isinstance(response.get("body"), str):
            view_model = extract_view_model(response["body"])

        if view_model is None:
            try:
                self.driver.get(FLYOUT_URL)
            except WebDriverException:
                return None
            self._pause(1.0, 1.8)
            view_model = self._read_local_model()

        if view_model is not None:
            self._remember(view_model)
        return view_model

    def offers(self):
        """The offers in the model last read."""
        return offers_from_view_model(self.view_model)

    def balance(self):
        """Available points from the model last read, or None."""
        return available_points(self.view_model)

    def offer(self, offer_id):
        """One offer from the model last read, or None."""
        for offer in self.offers():
            if offer["offer_id"] == offer_id:
                return offer
        return None

    # -- Reporting ------------------------------------------------------------

    def _on_flyout(self):
        """Whether the open page is the flyout the reports must come from."""
        try:
            url = (self.driver.current_url or "").casefold()
        except WebDriverException:
            return False
        return "panelflyout" in url

    def report(self, offer, stop_event=None):
        """
        Send one activity report for `offer`.

        Sent from the flyout page itself, so the request is same-origin and
        carries the sign-in cookies. The caller must have the flyout open; `load`
        and `refresh` both leave the browser there.

        Args:
            offer (dict): a record from `offers()`.
            stop_event (threading.Event, optional): aborts before the request.

        Returns:
            dict | None: {"status", "body"} as the page answered, or None.
        """
        if self._stopped(stop_event):
            return None

        # The report has to come from the flyout page: sent from anywhere else it
        # is a cross-origin request and the endpoint refuses it. A caller may
        # have navigated away (the page fallback opens articles), so this is
        # checked rather than assumed.
        if not self._on_flyout():
            try:
                self.driver.get(FLYOUT_URL)
            except WebDriverException as e:
                self.last_report_error = str(e).splitlines()[0][:140]
                return None
            self._pause(0.8, 1.5)

        body = report_body(offer, user_id=self._user_id)
        try:
            response = self.driver.execute_async_script(
                _REPORT_JS, REPORT_URL, json.dumps(body)
            )
        except WebDriverException as e:
            if not self._stopped(stop_event):
                self.last_report_error = str(e).splitlines()[0][:140]
                self._log(f"[WARNING] Activity report failed: {self.last_report_error}")
            return None

        return response if isinstance(response, dict) else None

    # -- The earning loop -----------------------------------------------------

    def earn(
        self, offers, stop_event=None, max_reports_per_offer=MAX_REPORTS_PER_OFFER
    ):
        """
        Report every offer given, and count only the ones Rewards confirms.

        An offer is worked until its own progress reaches its maximum (Read to
        Earn and punchcards take several reports) or a report does not move it.

        Args:
            offers (list): records from `offers()`.
            stop_event (threading.Event, optional): aborts between reports.
            max_reports_per_offer (int): guard against an offer that never moves.

        Returns:
            dict: {"attempted", "verified", "reports", "skipped"}, where
                `verified` counts offers Rewards' own data confirmed.
        """
        result = {"attempted": 0, "verified": 0, "reports": 0, "skipped": 0}

        for raw_offer in offers:
            if self._stopped(stop_event):
                break
            offer = normalise_offer(raw_offer)
            if not is_reportable(offer):
                result["skipped"] += 1
                continue

            result["attempted"] += 1
            offer_id = offer["offer_id"]
            title = offer.get("title") or offer_id

            # `pointProgressMax` is the offer's *points*, not a number of steps:
            # a Daily Set card reads 0/10 because it is worth ten points and is
            # earned in one report. So the loop is not "report max-progress
            # times"; it reports, looks, and stops as soon as the offer stops
            # moving or reaches its maximum. The cap only guards against an
            # offer that advances a little on every report forever.
            confirmed = 0
            for _ in range(max(1, int(max_reports_per_offer))):
                if self._stopped(stop_event):
                    break
                response = self.report(offer, stop_event=stop_event)
                result["reports"] += 1
                if not response_accepted(response):
                    status = response.get("status") if response else "?"
                    self._log(
                        f"  → '{title}': Rewards rejected the report (status {status})."
                    )
                    break

                self._pause(1.0, 2.0)
                self.refresh()
                current = self.offer(offer_id)
                if current is None:
                    # The offer left the list: complete, or the page changed
                    # shape. Neither is evidence of credit on its own.
                    break
                if not credit_moved(offer, current):
                    break

                confirmed += 1
                offer = current
                if current.get("complete"):
                    break
                if (
                    isinstance(current.get("maximum"), int)
                    and isinstance(current.get("progress"), int)
                    and current["progress"] >= current["maximum"]
                ):
                    break

            if confirmed:
                result["verified"] += 1
                self._log(f"  → '{title}': credited ({confirmed} report(s)).")
            elif is_reportable(offer):
                self._log(f"  → '{title}': reported, but Rewards did not advance it.")

        return result
