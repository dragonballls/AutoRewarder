"""Activities that earn points by being *used*, not merely opened.

Two kinds of earning live here, and they work differently.

**Paid for by the report.** "More Activities" cards, the Daily Set and quests are
one offer each, and Rewards records the credit when the activity is reported.
Send the report, re-read the offer list, and count it only if its progress moved.

**Paid for by being used.** Three tasks are not credited for a bare report:

  * **Read to Earn** — an article has to be opened and read before the report
    that claims it means anything. A report sent first is answered "accepted"
    and credits nothing, which is why this pass used to find a card and still
    never complete it.
  * **Explore on Bing** — the topics inside the card have to be opened, then the
    card claimed.
  * **Daily check-in** — a one-shot claim, once a day.

So Read and Explore run use → claim → verify, repeated while the offer needs
more reads or topics, and every count comes from the card's own progress rather
than from the pass's opinion of itself. The click-driven passes below remain for
a site whose offer list cannot be read at all.

Everything the module needs is passed in: it never imports the account layer, so
it can be exercised with a fake driver.
"""

import random
import re
import time

from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from .rewards_api import is_reportable

DASHBOARD_URL = "https://rewards.bing.com/dashboard"
EARN_URL = "https://rewards.bing.com/earn"

# Only these hosts are ever opened. A card's href is page-supplied data, so it
# is checked rather than trusted — the same rule the balance scraper follows.
_ALLOWED_HOSTS = ("bing.com", "rewards.bing.com", "msn.com", "microsoft.com")

# How long to keep an article on screen, in seconds. Reading credit is the one
# thing here that depends on elapsed time rather than on a click landing, and a
# person reading a short article spends roughly this long.
_READ_SECONDS = (14, 22)

# Scrolls spaced through that time, so the page sees movement rather than a
# single jump to the bottom.
_READ_SCROLL_STEPS = 4

# How many topics to open from an "Explore on Bing" card per attempt.
_MAX_TOPICS_PER_CARD = 6

# Read / Explore / check-in recognition. Titles are the reliable signal because
# these three cards carry them; the card text is included for cards whose title
# node is missing. Casefolding and collapsing punctuation keeps it working when
# the text is rendered a little differently.
_READ_PATTERNS = (
    r"\bread\s*(?:to|and)\s*earn\b",
    r"\bread\s+articles?\s+to\s+earn\b",
    r"\bread\s+(?:the\s+)?(?:latest\s+)?(?:stories|news|articles?)\b",
)
_EXPLORE_PATTERNS = (
    r"\bexplore\s+(?:on|with)\s+bing\b",
    r"\bexplore\s+(?:a\s+)?topic\s+(?:on\s+)?bing\b",
    r"\brelated\s+topics?\b",
)
_CHECK_IN_PATTERNS = (
    r"\bcheck\s*-?\s*in\b",
    r"\bdaily\s+check\s*-?\s*in\b",
)

# How many times an activity Rewards pays for by being used is repeated. Read to
# Earn needs one read per step, and a card is a handful of steps; this is a guard
# against a card that never advances, not a target.
_MAX_USE_CYCLES = 6

# Read the candidate cards from the two earn sections, plus any card anywhere on
# the page whose own text names one of the three activities (the dashboard does
# not always keep them inside #moreactivities). Read-only: this script queries
# and measures, and never clicks, submits or navigates.
_SCAN_JS = r"""
try {
  var out = [];
  var seen = {};

  function text(node) {
    return node ? (node.textContent || '').replace(/\s+/g, ' ').trim() : '';
  }

  function progressOf(card, anchor) {
    // A leaf node whose own text is exactly "N/M", so neighbouring badges
    // ("+50", "1/4") can't merge into one number the way card.innerText does.
    var nodes = (card || anchor).querySelectorAll('*');
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].children.length) continue;
      var m = text(nodes[i]).match(/^(\d+)\s*\/\s*(\d+)$/);
      if (m) return [parseInt(m[1], 10), parseInt(m[2], 10)];
    }
    return null;
  }

  function add(a, section) {
    if (!a || !a.href) return;
    var href = a.getAttribute('href') || '';
    if (!href) return;
    var card = a.closest(
      'article, li, [role="listitem"], [class*="card"], [class*="Card"]'
    ) || a;
    var cardText = text(card).slice(0, 1200);
    var titleNode = a.querySelector('.text-globalBody2Strong')
      || card.querySelector('h1, h2, h3, .text-globalBody2Strong');
    var title = (text(titleNode) || text(a) || a.getAttribute('aria-label') || '')
      .slice(0, 140);
    if (!title && !cardText) return;
    var key = section + '|' + href + '|' + title;
    if (seen[key]) return;
    seen[key] = true;
    var prog = progressOf(card, a);
    var points = cardText.match(/\+\s*(\d+)/);
    out.push({
      title: title,
      text: cardText,
      section: section,
      href: a.href || href,
      progress: prog ? (prog[0] + '/' + prog[1]) : null,
      completed: !!card.querySelector('[class*="statusSuccess"]'),
      points: points ? parseInt(points[1], 10) : null
    });
  }

  var specs = [
    ['#moreactivities a[href]', 'More Activities'],
    ['#dailyset a[href]', 'Daily Set']
  ];
  for (var s = 0; s < specs.length; s++) {
    var nodes = document.querySelectorAll(specs[s][0]);
    for (var i = 0; i < nodes.length; i++) add(nodes[i], specs[s][1]);
  }

  // Anywhere else on the page, but only when the card names one of the three
  // activities itself — otherwise this would list the whole dashboard.
  var keyword = new RegExp(
    'read\\s*(to|and)\\s*earn|read\\s+(the\\s+)?(latest\\s+)?(stories|news|articles?)|' +
    'explore\\s+(on|with)\\s+bing|related\\s+topics?|check\\s*-?\\s*in',
    'i'
  );
  var all = document.querySelectorAll('a[href]');
  for (var j = 0; j < all.length; j++) {
    var el = all[j];
    var around = el.closest(
      'article, li, [role="listitem"], [class*="card"], [class*="Card"]'
    ) || el;
    if (!keyword.test(text(around))) continue;
    add(el, 'Dashboard');
  }
  return out;
} catch (e) { return []; }
"""

# Read the topics inside an "Explore on Bing" hub. Each is a search link; the
# wrapper markup varies, so this takes every in-scope anchor pointing at a Bing
# search and de-duplicates by URL.
_TOPICS_JS = r"""
try {
  var out = [];
  var seen = {};
  var nodes = document.querySelectorAll(
    '[class*="rewardsTableAltBg"] a[href*="bing.com/search"],' +
    'a[href*="bing.com/search"]'
  );
  for (var i = 0; i < nodes.length; i++) {
    var a = nodes[i];
    var href = a.href || a.getAttribute('href') || '';
    if (!href || seen[href]) continue;
    if ((a.getAttribute('aria-disabled') || '') === 'true') continue;
    if ((a.getAttribute('data-disabled') || '') === 'true') continue;
    if (a.hasAttribute('disabled')) continue;
    seen[href] = 1;
    var t = a.querySelector('.text-globalBody2Strong') || a.querySelector('h3, p');
    out.push({
      href: href,
      title: (t ? t.textContent : a.textContent || '')
        .replace(/\s+/g, ' ').trim().slice(0, 120)
    });
  }
  return out;
} catch (e) { return []; }
"""

# Locate the daily check-in control, for the page fallback.
#
# Only a control whose own label says "check in" is accepted. The previous
# version also accepted *any* brand-styled control inside a card that mentions a
# streak, which is how the pass ended up clicking a "check in" label on some
# other tile, reporting "clicked, but the card did not confirm it" and leaving
# the real check-in unclaimed. A guess that can be wrong is worse than a clear
# "not offered": the report path above does the real work.
_CHECK_IN_JS = r"""
try {
  function text(node) {
    return node ? (node.textContent || '').replace(/\s+/g, ' ').trim() : '';
  }
  function usable(el) {
    if (!el) return false;
    if (!el.getClientRects().length) return false;
    if (el.disabled) return false;
    if ((el.getAttribute('aria-disabled') || '') === 'true') return false;
    if (el.hasAttribute('data-disabled')) return false;
    return true;
  }

  var label = /check\s*-?\s*in/i;
  var candidates = document.querySelectorAll('button, [role="button"], [data-react-aria-pressable]');
  for (var i = 0; i < candidates.length; i++) {
    if (!label.test(text(candidates[i]))) continue;
    if (!usable(candidates[i])) continue;
    // The control must sit in a card of its own, so its state can be read back.
    var card = candidates[i].closest(
      'article, li, [role="listitem"], [class*="card"], [class*="Card"]'
    );
    if (!card) continue;
    return {
      control: candidates[i],
      via: 'label',
      card: text(card).slice(0, 120),
      card_text: text(card).slice(0, 400)
    };
  }
  return null;
} catch (e) { return null; }
"""

# The check-in card's own state, read again after the click. `done` is the
# success badge; `text` is what the card says now, so the caller can see whether
# anything moved even when the markup carries no badge.
_CHECK_IN_STATE_JS = r"""
try {
  function text(node) {
    return node ? (node.textContent || '').replace(/\s+/g, ' ').trim() : '';
  }
  var streaks = /streak|s[ée]rie|racha|serie|check\s*-?\s*in/i;
  var cards = document.querySelectorAll('article, li, [role="listitem"], [class*="card"], [class*="Card"]');
  for (var i = 0; i < cards.length; i++) {
    if (!streaks.test(text(cards[i]))) continue;
    return {
      done: !!cards[i].querySelector('[class*="statusSuccess"]'),
      text: text(cards[i]).slice(0, 300)
    };
  }
  return null;
} catch (e) { return null; }
"""


def classify_earn_kind(title, text=""):
    """
    Return "read", "explore", "check_in" or None for a card.

    Used both to drive these activities and to keep them out of the generic
    "More Activities" click pass, which would otherwise open an article for
    three seconds, earn nothing, and still count it as an attempt.
    """
    combined = f"{title} {text}"

    for pattern in _READ_PATTERNS:
        if re.search(pattern, combined, re.IGNORECASE):
            return "read"
    for pattern in _EXPLORE_PATTERNS:
        if re.search(pattern, combined, re.IGNORECASE):
            return "explore"
    for pattern in _CHECK_IN_PATTERNS:
        if re.search(pattern, combined, re.IGNORECASE):
            return "check_in"
    return None


def offer_earn_kind(offer):
    """
    Which of the three earn-page activities an offer record belongs to.

    The offer list labels these by group as well as by title, and the group is
    the more reliable signal: a localized title can stop matching a pattern,
    while `dailyCheckInPromotion` stays the same field name. The title is used
    as the fallback, which keeps a renamed group working.

    Args:
        offer (dict): a record from `rewards_api.offers_from_view_model`.

    Returns:
        str | None: "read", "explore", "check_in" or None for everything else.
    """
    if not isinstance(offer, dict):
        return None

    source = str(offer.get("source") or "")
    if source == "dailyCheckInPromotion":
        return "check_in"
    if source == "exploreOnBingPromotions":
        return "explore"

    return classify_earn_kind(offer.get("title") or "")


def _offer_done(offer):
    """
    Whether an offer is finished, as the offer list says it.

    Rewards usually flips `complete` when the last step lands, but a card whose
    progress has reached its maximum is finished even if the flag has not caught
    up yet — the same rule `credit_moved` uses to call a report a success.
    """
    if not isinstance(offer, dict):
        return False
    if offer.get("complete"):
        return True
    progress, maximum = offer.get("progress"), offer.get("maximum")
    return (
        isinstance(progress, int)
        and isinstance(maximum, int)
        and maximum > 0
        and progress >= maximum
    )


def _parse_progress(value):
    """Turn "1/3" into (1, 3); (None, None) when it isn't a fraction."""
    match = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", str(value or ""))
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _is_allowed_url(url):
    """Whether a page-supplied href may be opened: http(s) on an allowed host."""
    if not isinstance(url, str):
        return False
    lowered = url.strip().casefold()
    if not lowered.startswith(("http://", "https://")):
        return False
    host = lowered.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    return any(
        host == allowed or host.endswith("." + allowed) for allowed in _ALLOWED_HOSTS
    )


class EarnActivities:
    """
    Drives Read to Earn, Explore on Bing and the daily check-in to completion.

    One instance per run. Every method is defensive: a page that doesn't match
    any expectation logs what it saw and returns zero, rather than raising into
    the run or reporting work that didn't happen.
    """

    def __init__(self, logger=None, read_seconds=None, pacing=True, earner=None):
        self.logger = logger
        # Real pauses between actions; tests turn this off so they don't spend
        # half a minute waiting for human-like delays they aren't testing.
        self._pacing = pacing
        # Reading time, in seconds. Overridable so tests don't wait out a real
        # reading session; production always uses the module default.
        self._read_seconds = read_seconds or _READ_SECONDS
        # The report-based earner, when the caller has one. This is what actually
        # credits Read to Earn and the check-in: both are decided server-side by
        # an activity report, not by how long a page was left open.
        self._earner = earner
        # Counts from the most recent run(), mirroring DailySet.last_totals so
        # the caller can fold them into the session's stats.
        self.last_totals = {
            "read": 0,
            "explore": 0,
            "check_in": 0,
        }
        # Whether those counts came from reports (verified) rather than from the
        # page-card fallback below.
        self.last_source = "none"

    def _log(self, message):
        if self.logger:
            self.logger(message)

    def _stopped(self, stop_event):
        return stop_event is not None and stop_event.is_set()

    def _sleep(self, seconds):
        """Pause, unless the caller disabled pacing (tests do)."""
        if self._pacing:
            time.sleep(max(0.0, float(seconds)))

    def _pause(self, low, high):
        """Pause for a random time in [low, high], subject to _sleep."""
        self._sleep(random.uniform(low, high))

    # -- Page reading ---------------------------------------------------------

    def _wait_ready(self, driver, timeout=15):
        """Wait until the new dashboard has hydrated enough to be read."""

        def _ready(d):
            try:
                return bool(
                    d.execute_script(
                        "return !!(window.__next_f || document.getElementById('dailyset')"
                        " || document.getElementById('moreactivities'));"
                    )
                )
            except WebDriverException:
                return False

        try:
            WebDriverWait(driver, timeout).until(_ready)
        except TimeoutException:
            pass

    def _open_earn_page(self, driver):
        """Load /earn, expanding collapsed sections so their cards can be read."""
        try:
            driver.get(EARN_URL)
        except WebDriverException as e:
            self._log(f"[WARNING] Could not open the earn page: {e}")
            return False

        self._wait_ready(driver)
        self._pause(1.5, 2.5)
        try:
            triggers = driver.find_elements(By.CSS_SELECTOR, "button[slot='trigger']")
        except WebDriverException:
            triggers = []
        for button in triggers:
            try:
                if (button.get_attribute("aria-expanded") or "").lower() == "false":
                    driver.execute_script("arguments[0].click();", button)
                    self._pause(0.4, 0.9)
            except WebDriverException:
                continue
        return True

    def _scan(self, driver):
        """Every candidate earn card on the page, as plain dicts."""
        try:
            raw = driver.execute_script(_SCAN_JS)
        except WebDriverException as e:
            self._log(f"[WARNING] Could not read the earn page: {e}")
            return []
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    def _pending_of_kind(self, driver, kind):
        """
        Cards of `kind` that still have work left.

        A card counts as pending when it isn't marked complete and either has no
        progress counter (nothing to compare against) or hasn't reached the end
        of one. Completed cards are ignored, so a second run the same day does
        nothing.
        """
        pending = []
        for card in self._scan(driver):
            if classify_earn_kind(card.get("title"), card.get("text")) != kind:
                continue
            if card.get("completed"):
                continue
            done, total = _parse_progress(card.get("progress"))
            if total is not None and done is not None and done >= total:
                continue
            pending.append(card)
        return pending

    def _progress_for(self, driver, href, title):
        """Re-read one card's progress, to see whether an action moved it."""
        for card in self._scan(driver):
            same_href = href and card.get("href") == href
            same_title = title and card.get("title") == title
            if not (same_href or same_title):
                continue
            done, total = _parse_progress(card.get("progress"))
            return done, total, bool(card.get("completed"))
        return None, None, None

    # -- Interaction ----------------------------------------------------------

    def _locate(self, driver, href):
        """Find the card's own anchor by exact href, so a real click can be used."""
        try:
            anchors = driver.find_elements(By.CSS_SELECTOR, "a[href]")
        except WebDriverException:
            return None
        for anchor in anchors:
            try:
                if (anchor.get_attribute("href") or "") == href:
                    if anchor.getClientRects():
                        return anchor
            except WebDriverException:
                continue
        return None

    def _open_card(self, driver, human, href, main_tab, return_url, stop_event=None):
        """
        Open a card the way a person does and leave the browser back on the earn
        page.

        A real pointer click is used when the card can be found, because the
        click is what the page reacts to; otherwise the URL is opened directly,
        which is weaker but still better than skipping the activity. Returns
        True when something was opened.
        """
        if not _is_allowed_url(href):
            self._log(f"[WARNING] Refusing to open an unexpected address: {href!r}")
            return False

        anchor = self._locate(driver, href)
        before = set()
        try:
            before = set(driver.window_handles)
        except WebDriverException:
            pass

        clicked = False
        if anchor is not None:
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center', inline:'nearest'});",
                    anchor,
                )
                self._pause(0.4, 0.8)
                human.click_element(anchor, scroll_into_view=False)
                clicked = True
                self._pause(2, 3.5)
            except WebDriverException as e:
                if self._stopped(stop_event):
                    return False
                self._log(
                    f"[INFO] Card click failed ({str(e).splitlines()[0][:80]}); opening directly."
                )

        if not clicked:
            try:
                driver.get(href)
                self._pause(2, 3.5)
            except WebDriverException as e:
                if self._stopped(stop_event):
                    return False
                self._log(f"[WARNING] Could not open the activity: {e}")
                return False

        # A card may open its destination in a new tab; adopt it so the reading
        # and scrolling happen on the page Rewards is watching.
        try:
            new_tabs = [
                handle
                for handle in driver.window_handles
                if handle not in before and handle != main_tab
            ]
            if new_tabs:
                driver.switch_to.window(new_tabs[-1])
        except WebDriverException:
            pass

        return True

    def _close_back(self, driver, main_tab, return_url, stop_event=None):
        """Close whatever was opened and return to the earn page."""
        if self._stopped(stop_event):
            return
        try:
            current = driver.current_window_handle
        except WebDriverException:
            current = None

        if current is not None and current != main_tab:
            try:
                driver.close()
            except WebDriverException:
                pass
            try:
                driver.switch_to.window(main_tab)
            except WebDriverException:
                return
            self._pause(0.8, 1.5)
            return

        # Same-tab navigation: go back to the page the card came from.
        try:
            if return_url and driver.current_url != return_url:
                driver.get(return_url)
                self._wait_ready(driver)
                self._pause(1.2, 2)
        except WebDriverException:
            pass

    def _read_article(self, driver, human, stop_event=None):
        """
        Read the open article for long enough to earn credit.

        Credit here depends on elapsed time on the page, not on a click, so this
        scrolls in steps through a realistic reading period rather than parking
        on the page and immediately closing it.
        """
        target = random.uniform(*self._read_seconds)
        step = target / max(1, _READ_SCROLL_STEPS)

        deadline = time.monotonic() + target
        for _ in range(_READ_SCROLL_STEPS):
            if self._stopped(stop_event):
                return
            try:
                human.scroll_page()
            except WebDriverException:
                pass
            remaining = max(0.0, min(step, deadline - time.monotonic()))
            if self._stopped(stop_event):
                return
            self._sleep(remaining)

        # A short settle so the credit beacon has time to fire before the tab
        # closes, which is the difference between reading and having read.
        if not self._stopped(stop_event):
            self._pause(2, 3.5)

    def _explore_topics(self, driver, human, href, stop_event=None):
        """
        Open the topics inside an "Explore on Bing" hub.

        The hub lists search links; opening them is what advances the card. The
        topics are read from the hub itself rather than from the card, because
        that's where they live.

        Returns:
            int: how many topics were opened.
        """
        try:
            topics = driver.execute_script(_TOPICS_JS)
        except WebDriverException as e:
            self._log(f"[INFO] Could not read the explore topics: {e}")
            return 0
        if not isinstance(topics, list):
            return 0

        usable = [
            topic
            for topic in topics
            if isinstance(topic, dict) and _is_allowed_url(topic.get("href"))
        ]
        # Never open the hub's own address again.
        usable = [topic for topic in usable if topic.get("href") != href]
        if not usable:
            self._log("[INFO] No topics found inside the Explore card.")
            return 0

        opened = 0
        try:
            main_tab = driver.current_window_handle
        except WebDriverException:
            return 0

        for topic in usable[:_MAX_TOPICS_PER_CARD]:
            if self._stopped(stop_event):
                break
            before = set()
            try:
                before = set(driver.window_handles)
            except WebDriverException:
                pass

            label = topic.get("title") or "topic"
            self._log(f"  → exploring topic: {label}")
            try:
                driver.get(topic["href"])
                opened += 1
                self._pause(3, 6)
                try:
                    human.scroll_page()
                except WebDriverException:
                    pass
                self._pause(1.5, 3)
            except WebDriverException as e:
                if self._stopped(stop_event):
                    break
                self._log(f"[INFO] Topic did not open ({str(e).splitlines()[0][:70]}).")
                continue

            # Any extra tab the topic spawned is collateral; close it.
            try:
                for handle in driver.window_handles:
                    if handle not in before:
                        driver.switch_to.window(handle)
                        driver.close()
                driver.switch_to.window(main_tab)
            except WebDriverException:
                pass

        return opened

    # -- The three activities -------------------------------------------------

    # -- Report-based passes --------------------------------------------------

    def run_by_report(self, driver, human=None, stop_event=None):
        """
        Complete Read to Earn, Explore on Bing and the daily check-in, and count
        only what Rewards confirms.

        Two different things are called "earning" here, and they need opposite
        orders.

        The check-in, the Daily Set and an ordinary offer are paid for by the
        report itself, so reporting is the whole action: send it, re-read, and
        count the offer only if it moved.

        Read to Earn and Explore on Bing pay for being **used**. The report does
        not perform the activity, it claims one that has already happened — so
        the article has to be read first, or the topic opened, and only then is
        the report sent. For an offer that needs three reads there are three of
        those cycles. Doing it the other way round is what this pass used to do:
        send a report at progress 0, be told "accepted", watch nothing move, and
        report a card it had found as one it could not complete.

        Args:
            driver: Selenium WebDriver on a signed-in Rewards profile.
            human: required to perform Read/Explore; without it those two are
                claimed but not performed, which credits nothing.
            stop_event (threading.Event, optional): aborts between reports.

        Returns:
            dict | None: {read, explore, check_in} verified counts, or None when
                there is no earner or the offer list could not be read (the
                caller then runs the page-driven pass alone).
        """
        if self._earner is None:
            return None

        if self._earner.view_model is None:
            if self._earner.load(navigate=True) is None:
                return None

        totals = {"read": 0, "explore": 0, "check_in": 0}

        for kind, label, page_runner in (
            ("read", "Read to Earn", self.run_read_to_earn),
            ("explore", "Explore on Bing", self.run_explore),
            ("check_in", "Daily check-in", self.run_check_in),
        ):
            if self._stopped(stop_event):
                break

            pending = [
                offer
                for offer in self._earner.offers()
                if offer_earn_kind(offer) == kind
            ]
            todo = [offer for offer in pending if is_reportable(offer)]
            if not todo:
                if pending:
                    self._log(f"{label}: already complete.")
                else:
                    self._log(f"{label}: nothing pending.")
                continue

            plural = "offer" if len(todo) == 1 else "offers"
            self._log(f"{label}: {len(todo)} {plural} to earn.")

            if kind in ("read", "explore") and human is not None:
                # These two pay for being *used*: Rewards credits an article
                # that was read and a topic that was opened, then pays when the
                # activity is claimed. So the order is use → claim → verify, and
                # for an offer that needs several reads the three repeat. Firing
                # one report first and only then reading was backwards, and it is
                # why a card could be found and still never complete.
                totals[kind] = self._earn_by_use(
                    driver, human, kind, label, page_runner, stop_event
                )
            else:
                result = self._earner.earn(todo, stop_event=stop_event)
                totals[kind] = int(result.get("verified", 0) or 0)

            if not totals[kind]:
                self._log(
                    f"[WARNING] {label}: Rewards confirms no credit for it this " "run."
                )

        self.last_totals = dict(totals)
        self.last_source = "report"
        self._log(
            "Earn activities: "
            f"read {totals['read']}, "
            f"explore {totals['explore']}, "
            f"check-in {totals['check_in']}."
        )
        return dict(totals)

    def _offers_of_kind(self, kind):
        """The pending offers of one activity, from the model last read."""
        return [
            offer
            for offer in self._earner.offers()
            if offer_earn_kind(offer) == kind and not _offer_done(offer)
        ]

    def _earn_by_use(self, driver, human, kind, label, runner, stop_event):
        """
        Earn an activity Rewards pays for by being used: use it, claim it, check.

        Read to Earn and Explore on Bing are not paid for a bare report — the
        report claims what was already done. So each cycle opens the activity on
        the earn page (reading an article, opening a topic), asks Rewards to
        claim it, and re-reads the offer list. An offer needing several reads
        takes several cycles, which is why this repeats until it completes or
        stops moving.

        Args:
            driver: Selenium WebDriver on a signed-in Rewards profile.
            human: HumanBehavior, for the clicking and scrolling.
            kind (str): "read" or "explore".
            label (str): the name to put in the log.
            runner (callable): the page pass that performs the activity.
            stop_event (threading.Event, optional): aborts between cycles.

        Returns:
            int: offers of this kind Rewards now says are complete.
        """
        completed_before = {
            offer["offer_id"]
            for offer in self._earner.offers()
            if offer_earn_kind(offer) == kind and _offer_done(offer)
        }

        for cycle in range(1, _MAX_USE_CYCLES + 1):
            if self._stopped(stop_event):
                break
            pending = self._offers_of_kind(kind)
            if not pending:
                break

            if cycle == 1:
                self._log(f"{label}: earning it by use, then claiming it.")
            else:
                self._log(f"{label}: still open, going again ({cycle}).")

            before = {offer["offer_id"]: offer.get("progress") for offer in pending}

            # 1. Use it. This is the part Rewards wants to have happened — the
            #    article read, the topic opened — before the report claims it.
            #    One pass per cycle: this loop is the repetition, so the pass
            #    must not run its own, or one read becomes five.
            if not self._open_earn_page(driver):
                self._log(f"[WARNING] {label}: could not open the earn page.")
                return 0
            try:
                runner(driver, human, stop_event=stop_event, once=True)
            except TypeError:
                # A pass that does not take `once` is not ours to repeat.
                try:
                    runner(driver, human, stop_event=stop_event)
                except Exception as e:
                    if self._stopped(stop_event):
                        break
                    self._log(
                        f"[WARNING] {label} page pass failed: {type(e).__name__}: {e}"
                    )
            except Exception as e:
                if self._stopped(stop_event):
                    break
                self._log(
                    f"[WARNING] {label} page pass failed: {type(e).__name__}: {e}"
                )

            if self._stopped(stop_event):
                break

            # 2. Claim it. The report is what records the credit, and it is sent
            #    after the use rather than before: a report for an activity that
            #    has not happened is answered "accepted" and credits nothing,
            #    which is the response this whole pass used to be fooled by.
            #    The list is re-read first so the report carries a live `hash`.
            self._earner.refresh()
            claimable = [
                offer for offer in self._offers_of_kind(kind) if is_reportable(offer)
            ]
            if claimable:
                self._earner.earn(claimable, stop_event=stop_event)

            # 3. Believe only the offer list, and only a counter that moved.
            self._earner.refresh()
            remaining = self._offers_of_kind(kind)
            if not remaining:
                break

            moved = any(
                offer.get("progress") != before.get(offer["offer_id"])
                for offer in remaining
            )
            if not moved:
                # Another round would read the same article to the same effect.
                # Say so, rather than looping until the cap.
                self._log(
                    f"[INFO] {label}: the activity did not advance the offer, so "
                    "another round would not either."
                )
                break

        after = {
            offer["offer_id"]
            for offer in self._earner.offers()
            if offer_earn_kind(offer) == kind and _offer_done(offer)
        }
        return len(after - completed_before)

    def run_read_to_earn(self, driver, human, stop_event=None, once=False):
        """
        Complete every pending Read to Earn card.

        Args:
            driver: Selenium WebDriver on a signed-in Rewards profile.
            human: HumanBehavior, for the clicking and scrolling.
            stop_event (threading.Event, optional): aborts between reads.
            once (bool): read each card a single time and stop, leaving the
                repetition to the caller. `run_by_report` cycles read → claim →
                verify itself, so having this loop as well would read every
                article several times over.

        Returns:
            int: cards whose progress advanced after reading.
        """
        pending = self._pending_of_kind(driver, "read")
        if not pending:
            self._log("Read to Earn: nothing pending.")
            return 0

        self._log(f"Read to Earn: {len(pending)} card(s) to complete.")
        try:
            main_tab = driver.current_window_handle
        except WebDriverException:
            return 0

        completed = 0
        for card in pending:
            if self._stopped(stop_event):
                break
            href = card.get("href")
            title = card.get("title") or "Read to Earn"
            before_done, before_total = _parse_progress(card.get("progress"))
            readings = max(1, (before_total or 1) - (before_done or 0))
            if once:
                readings = 1

            # A card asking for 3 reads needs the article opened 3 times, so the
            # loop is driven by the counter rather than by one attempt per card.
            for attempt in range(min(readings, 5)):
                if self._stopped(stop_event):
                    break
                self._log(
                    f"Reading for '{title}' "
                    f"({(before_done or 0) + attempt + 1}/{before_total or '?'})…"
                )
                if not self._open_card(
                    driver, human, href, main_tab, EARN_URL, stop_event
                ):
                    break
                self._read_article(driver, human, stop_event=stop_event)
                self._close_back(driver, main_tab, EARN_URL, stop_event=stop_event)

                after_done, _, complete = self._progress_for(
                    driver, href, card.get("title")
                )
                if complete:
                    completed += 1
                    self._log(f"Read to Earn: '{title}' verified complete.")
                    break
                if (
                    after_done is not None
                    and before_done is not None
                    and after_done <= before_done + attempt
                ):
                    self._log(
                        f"[WARNING] Read to Earn: '{title}' did not advance "
                        f"after reading (still {after_done})."
                    )
                    break

            # A card with no counter can't be verified, so it is not counted;
            # say so rather than implying it credited.
            if before_total is None:
                self._log(
                    f"[INFO] Read to Earn: '{title}' has no progress counter, "
                    "so credit cannot be verified."
                )

        return completed

    def run_explore(self, driver, human, stop_event=None):
        """
        Complete every pending Explore on Bing card by opening its topics.

        Returns:
            int: cards whose progress advanced.
        """
        pending = self._pending_of_kind(driver, "explore")
        if not pending:
            self._log("Explore on Bing: nothing pending.")
            return 0

        self._log(f"Explore on Bing: {len(pending)} card(s) to complete.")
        try:
            main_tab = driver.current_window_handle
        except WebDriverException:
            return 0

        completed = 0
        for card in pending:
            if self._stopped(stop_event):
                break
            href = card.get("href")
            title = card.get("title") or "Explore on Bing"
            before_done, before_total = _parse_progress(card.get("progress"))

            self._log(f"Exploring for '{title}'…")
            if not self._open_card(driver, human, href, main_tab, EARN_URL, stop_event):
                continue

            opened = self._explore_topics(driver, human, href, stop_event=stop_event)
            if not opened:
                self._log(
                    f"[WARNING] Explore on Bing: '{title}' offered no topics to open."
                )

            self._close_back(driver, main_tab, EARN_URL, stop_event=stop_event)

            after_done, _, complete = self._progress_for(
                driver, href, card.get("title")
            )
            if complete or (
                after_done is not None
                and before_done is not None
                and after_done > before_done
            ):
                completed += 1
                self._log(f"Explore on Bing: '{title}' verified complete.")

        return completed

    def run_check_in(self, driver, human, stop_event=None):
        """
        Claim today's check-in from the page, when the offer list can't be read.

        This is the fallback. The real pass reports the check-in offer like any
        other activity, which is what Rewards pays for; this path only clicks a
        control whose own label says "check in" and then reads that card's state
        back. It never reports success from the click.

        Returns:
            int: 1 when the click was made and the card changed, else 0.
        """
        try:
            info = driver.execute_script(_CHECK_IN_JS)
        except WebDriverException as e:
            self._log(f"[INFO] Could not look for a check-in card: {e}")
            return 0

        if not isinstance(info, dict) or not info.get("control"):
            self._log("Daily check-in: not offered today.")
            return 0

        before = None
        try:
            before = driver.execute_script(_CHECK_IN_STATE_JS)
        except WebDriverException:
            before = None

        if isinstance(before, dict) and before.get("done"):
            self._log("Daily check-in: already claimed today.")
            return 0

        self._log(
            f"Daily check-in: clicking the control (found via {info.get('via')})."
        )
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", info["control"]
            )
            self._pause(0.4, 0.8)
            human.click_element(info["control"], scroll_into_view=False)
            self._pause(2, 3.5)
        except WebDriverException as e:
            if self._stopped(stop_event):
                return 0
            self._log(
                f"[WARNING] Daily check-in click failed: {str(e).splitlines()[0][:90]}"
            )
            return 0

        after = None
        try:
            after = driver.execute_script(_CHECK_IN_STATE_JS)
        except WebDriverException:
            after = None

        if isinstance(after, dict):
            if after.get("done"):
                self._log("Daily check-in: verified claimed.")
                return 1
            if isinstance(before, dict) and after.get("text") != before.get("text"):
                # The card changed but carries no success badge we recognise;
                # the change is the evidence available.
                self._log("Daily check-in: the card updated after the click.")
                return 1

        self._log(
            "[WARNING] Daily check-in: clicked, but the card did not confirm it. "
            "Not counting it as complete."
        )
        return 0

    # -- Entry point ----------------------------------------------------------

    def run(self, driver, human, stop_event=None):
        """
        Do every earn-page activity this module owns.

        Each pass is isolated: a page that doesn't offer one activity, or one
        that fails outright, must not stop the others from being attempted — the
        point is to collect every point available today.

        The report pass runs first and, when Rewards' offer list answers, it is
        the whole job — including reading the articles Read to Earn needs. The
        page-driven passes below stay for a site whose offer list cannot be read;
        they can open a card but cannot tell whether anything was credited.

        Args:
            driver: Selenium WebDriver on a signed-in Rewards profile.
            human: HumanBehavior instance used for the clicks.
            stop_event (threading.Event, optional): aborts between steps.

        Returns:
            dict: verified counts, mirroring DailySet.last_totals.
        """
        self.last_totals = {"read": 0, "explore": 0, "check_in": 0}

        if not self._stopped(stop_event):
            reported = self.run_by_report(driver, human, stop_event=stop_event)
            if reported is not None:
                return reported

        self.last_source = "page"
        if not self._open_earn_page(driver):
            return dict(self.last_totals)

        for key, name, runner in (
            ("read", "Read to Earn", self.run_read_to_earn),
            ("explore", "Explore on Bing", self.run_explore),
            ("check_in", "Daily check-in", self.run_check_in),
        ):
            if self._stopped(stop_event):
                self._log(f"{name}: stopped before it ran.")
                break
            try:
                count = runner(driver, human, stop_event=stop_event)
            except Exception as e:  # one broken pass must not lose the others
                if self._stopped(stop_event):
                    break
                self._log(f"[WARNING] {name} pass failed: {type(e).__name__}: {e}")
                continue
            self.last_totals[key] = int(count or 0)

        self._log(
            "Earn activities: "
            f"read {self.last_totals['read']}, "
            f"explore {self.last_totals['explore']}, "
            f"check-in {self.last_totals['check_in']}."
        )
        return dict(self.last_totals)
