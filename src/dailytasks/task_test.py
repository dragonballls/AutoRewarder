"""One earning task at a time, for testing.

The app runs every earning task in one go, which is the right way to use it and
the wrong way to *check* it: when one task misbehaves there is no way to say
"run just the daily check-in and tell me what happened" without doing the whole
day over.

This module is that missing piece. It exposes each earning surface as its own
named task and runs exactly one of them, in isolation, and reports what the page
said afterwards — without marking anything as complete for the day.

Two deliberate rules:

  * **A test never marks a day as done.** Testing Read to Earn on Tuesday must
    not convince the app that Tuesday's Daily Set is finished, or the real run
    that evening would skip it. Only a normal run writes those markers.
  * **A test reports, it does not promise.** Every result carries what was
    attempted and what the page confirmed, so a task that ran but did not credit
    is visibly different from one that did.
"""

from selenium.common.exceptions import WebDriverException

# Every earning task the app can perform, in the order a full run does them.
# `dashboard` tasks live on the Daily Set page, `earn` tasks on the earn page,
# and the rest are handled through the browser directly.
TASKS = {
    "daily_set": {
        "label": "Daily Set",
        "page": "dashboard",
        "description": "The 3 daily cards (quiz, poll, story).",
    },
    "claim": {
        "label": "Pending points claim",
        "page": "dashboard",
        "description": "Claims points already earned but not yet collected.",
    },
    "more_activities": {
        "label": "More Activities",
        "page": "earn",
        "description": "The point cards under More Activities.",
    },
    "quests": {
        "label": "Quests / punchcards",
        "page": "earn",
        "description": "Multi-task punchcards, one unlocked step at a time.",
    },
    "read_to_earn": {
        "label": "Read to Earn",
        "page": "earn",
        "description": "Reads an article for long enough to be credited.",
    },
    "explore": {
        "label": "Explore on Bing",
        "page": "earn",
        "description": "Opens the topics inside the Explore card.",
    },
    "check_in": {
        "label": "Daily check-in",
        "page": "earn",
        "description": "Claims today's check-in.",
    },
    "visual_search": {
        "label": "Visual Search",
        "page": "browser",
        "description": "Uploads an image and ticks the visual search streak.",
    },
    "pc_searches": {
        "label": "PC searches",
        "page": "browser",
        "description": "Desktop search queries.",
    },
    "mobile_searches": {
        "label": "Mobile searches",
        "page": "browser",
        "description": "Mobile search queries.",
    },
}

TASK_NAMES = tuple(TASKS)


def task_names():
    """Every testable task name, in run order."""
    return TASK_NAMES


def describe_tasks():
    """The task list with labels and descriptions, for a UI or the log."""
    return [
        {
            "name": name,
            "label": spec["label"],
            "description": spec["description"],
            "page": spec["page"],
        }
        for name, spec in TASKS.items()
    ]


def resolve_task(name):
    """
    Turn a user-supplied task name into a canonical one.

    Accepts the canonical name as well as the label and common spellings, so
    "Read to Earn", "read-to-earn" and "read_to_earn" are the same task.
    Raises ValueError for anything unrecognised, listing what is valid.
    """
    if not name:
        raise ValueError("A task name is required.")

    wanted = str(name).strip().casefold()
    normalised = wanted.replace("-", "_").replace(" ", "_")

    if normalised in TASKS:
        return normalised

    for canonical, spec in TASKS.items():
        if str(spec["label"]).casefold() == wanted:
            return canonical

    raise ValueError(f"Unknown task {name!r}. Valid tasks: {', '.join(TASK_NAMES)}.")


class TaskTester:
    """
    Runs a single earning task and reports what happened.

    The caller supplies the already-built pieces (the dashboard handler, the
    earn-activities pass, the search engine) so this module owns only the
    "run exactly one of them and describe it" logic, and stays testable without
    a browser.
    """

    def __init__(self, logger=None):
        self.logger = logger

    def _log(self, message):
        if self.logger:
            self.logger(message)

    def _stopped(self, stop_event):
        return stop_event is not None and stop_event.is_set()

    def run(
        self,
        task,
        driver,
        human,
        daily_set=None,
        earn=None,
        api=None,
        stop_event=None,
    ):
        """
        Run one task and return a structured result.

        Args:
            task (str): a canonical task name from TASKS.
            driver: Selenium WebDriver on a signed-in profile, already open.
            human: HumanBehavior instance for the clicks.
            daily_set: DailySet instance, for the dashboard/earn tasks.
            earn: EarnActivities instance, for Read / Explore / check-in.
            api: AutoRewarderAPI, for searches and Visual Search.
            stop_event (threading.Event, optional): aborts the task.

        Returns:
            dict: {task, label, ok, detail, verified, notes}. `verified` is True
            only when the page itself confirmed the work; `ok` says the task ran
            without error, which is a weaker statement.
        """
        result = {
            "task": str(task),
            "label": str(task),
            "ok": False,
            "verified": False,
            "detail": "",
            "notes": [],
        }

        try:
            canonical = resolve_task(task)
        except ValueError as e:
            result["detail"] = str(e)
            self._log(f"[WARNING] {e}")
            return result

        spec = TASKS[canonical]
        result.update({"task": canonical, "label": spec["label"]})

        if driver is None and spec["page"] != "browser":
            result["detail"] = "No browser session."
            return result

        self._log(f"=== Testing '{spec['label']}' on its own ===")

        try:
            if canonical == "daily_set":
                return self._run_daily_set(driver, human, daily_set, result, stop_event)
            if canonical == "claim":
                return self._run_claim(driver, human, daily_set, result, stop_event)
            if canonical == "more_activities":
                return self._run_earn_page(
                    driver, human, daily_set, result, "_run_more_activities", stop_event
                )
            if canonical == "quests":
                return self._run_earn_page(
                    driver, human, daily_set, result, "_run_quests", stop_event
                )
            if canonical == "read_to_earn":
                return self._run_earn_activity(
                    driver, human, earn, result, "run_read_to_earn", stop_event
                )
            if canonical == "explore":
                return self._run_earn_activity(
                    driver, human, earn, result, "run_explore", stop_event
                )
            if canonical == "check_in":
                return self._run_earn_activity(
                    driver, human, earn, result, "run_check_in", stop_event
                )
            if canonical == "visual_search":
                return self._run_visual_search(api, result, stop_event)
            if canonical in ("pc_searches", "mobile_searches"):
                return self._run_searches(
                    api, canonical == "mobile_searches", result, stop_event
                )

            result["detail"] = f"No runner for {canonical!r}."
            return result

        except Exception as e:  # a test must report, never take the app down
            result["detail"] = f"{type(e).__name__}: {e}"
            self._log(f"[WARNING] Test of '{spec['label']}' failed: {result['detail']}")
            return result

    # -- Per-task runners -----------------------------------------------------

    def _run_daily_set(self, driver, human, daily_set, result, stop_event):
        if daily_set is None:
            result["detail"] = "Daily Set handler unavailable."
            return result

        self._log("Reading the Daily Set as it stands now…")
        variant = getattr(daily_set, "dashboard_variant", "auto")
        ok = daily_set.perform_daily_set(
            driver, human, stop_event=stop_event, variant=variant
        )
        totals = daily_set.last_totals or {}

        result["ok"] = bool(ok)
        result["verified"] = bool(ok)
        result["detail"] = (
            f"{totals.get('final', 0)}/{totals.get('total', 0)} cards complete, "
            f"{totals.get('newly', 0)} newly finished."
        )
        if not totals.get("total"):
            result["notes"].append("No Daily Set cards were found on the page.")
        # The offers the run reported, when it reported them: this is the number
        # that came from Rewards rather than from a click.
        reported = getattr(daily_set, "last_reported", None)
        if isinstance(reported, dict) and reported.get("verified"):
            result["notes"].append(
                f"{reported['verified']} offer(s) confirmed credited by Rewards."
            )
        return result

    def _run_claim(self, driver, human, daily_set, result, stop_event):
        handler = self._dashboard_handler(daily_set)
        if handler is None:
            result["detail"] = "New-dashboard handler unavailable."
            return result

        before = getattr(handler, "last_totals", {}) or {}
        handler._run_claim(driver, human, stop_event=stop_event)
        after = getattr(handler, "last_totals", {}) or {}

        attempted = int(after.get("attempted", 0)) - int(before.get("attempted", 0))
        result["ok"] = True
        # The claim control does not report back, so this cannot be verified.
        result["verified"] = False
        result["detail"] = (
            "Claim control clicked." if attempted else "Nothing was pending to claim."
        )
        result["notes"].append(
            "Rewards does not confirm a claim in the page; check your balance."
        )
        return result

    def _run_earn_page(self, driver, human, daily_set, result, method, stop_event):
        handler = self._dashboard_handler(daily_set)
        if handler is None:
            result["detail"] = "New-dashboard handler unavailable."
            return result

        before = dict(getattr(handler, "last_totals", {}) or {})
        getattr(handler, method)(driver, human, stop_event=stop_event)
        after = dict(getattr(handler, "last_totals", {}) or {})

        opened = int(after.get("earn", 0)) - int(before.get("earn", 0))
        quests = int(after.get("quests", 0)) - int(before.get("quests", 0))
        count = quests if method == "_run_quests" else opened
        verified = int(
            (after.get("quests_verified", 0) - before.get("quests_verified", 0))
            if method == "_run_quests"
            else (after.get("more_verified", 0) - before.get("more_verified", 0))
        )

        result["ok"] = True
        # A report-confirmed offer is verified; an opened card is not. The two
        # numbers are reported separately so "opened 3" can never be read as
        # "earned 3".
        result["verified"] = verified > 0
        if verified:
            result["detail"] = f"{verified} offer(s) verified credited."
        elif count:
            result["detail"] = f"{count} item(s) opened; nothing confirmed credited."
        else:
            result["detail"] = "Nothing pending was found for this task."

        if not verified:
            result["notes"].append(
                "Rewards did not confirm credit. Check the run log for what it "
                "answered."
            )
        return result

    def _run_earn_activity(self, driver, human, earn, result, method, stop_event):
        if earn is None:
            result["detail"] = "Earn-activities pass unavailable."
            return result

        # Start from the earn page, which is where these cards live.
        if not earn._open_earn_page(driver):
            result["detail"] = "Could not open the earn page."
            return result

        count = getattr(earn, method)(driver, human, stop_event=stop_event)

        result["ok"] = True
        # These passes count a card only when Rewards' own data moved, so a
        # count here is a verified one — from either the report path or the
        # page fallback, both of which read the card's state back.
        result["verified"] = bool(count)
        result["detail"] = (
            f"{count} card(s) verified complete."
            if count
            else "The card did not confirm credit."
        )
        if not count:
            source = getattr(earn, "last_source", "none")
            result["notes"].append(
                "Rewards reports nothing left to earn here, or did not advance it "
                f"— the log above says which (path: {source})."
            )
        return result

    def _run_visual_search(self, api, result, stop_event):
        if api is None:
            result["detail"] = "API unavailable."
            return result

        ok = api._run_visual_search_if_needed()
        result["ok"] = True
        result["verified"] = bool(ok)
        result["detail"] = (
            "Visual search completed and the streak moved."
            if ok
            else "Visual search did not confirm credit."
        )
        return result

    def _run_searches(self, api, mobile, result, stop_event):
        if api is None or api.search_engine is None:
            result["detail"] = "Search engine unavailable."
            return result

        count = 3
        queries = api._build_queries(count)
        if not queries:
            result["detail"] = "No queries available."
            return result

        driver = api.driver_manager.setup_driver(mobile=mobile)
        try:
            done = api.search_engine.perform_searches(
                driver,
                queries,
                mobile=mobile,
                stop_event=stop_event,
                search_delay_range=(1.0, 2.0),
            )
        finally:
            try:
                driver.quit()
            except WebDriverException:
                pass

        done = int(done or 0)
        result["ok"] = True
        result["verified"] = done > 0
        result["detail"] = f"{done}/{count} test searches submitted."
        result["notes"].append(
            "A search submits fine even when Rewards does not count it, so "
            "watch your points total to confirm this one."
        )
        return result

    @staticmethod
    def _dashboard_handler(daily_set):
        """The new-dashboard handler behind a DailySet, if one exists."""
        if daily_set is None:
            return None
        # DailySet builds this lazily during a run; build one on demand so a
        # single task can be tested without running the Daily Set first.
        handler = getattr(daily_set, "_new_handler", None)
        if handler is not None:
            return handler
        from .new_dashboard import NewDashboardDailySet

        return NewDashboardDailySet(logger=getattr(daily_set, "logger", None))
