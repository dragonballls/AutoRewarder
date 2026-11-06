"""Read-only Microsoft Rewards activity discovery and manual-review helpers.

The browser scanner only reads the two dashboard pages already used by
AutoRewarder. It does not click activity cards, submit searches, open articles,
answer quizzes/polls, claim points, or change account state.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

DASHBOARD_URL = "https://rewards.bing.com/dashboard"
EARN_URL = "https://rewards.bing.com/earn"


class ActivityKind(str, Enum):
    READ_TO_EARN = "read_to_earn"
    EXPLORE = "explore"
    DAILY_SET = "daily_set"
    PUNCHCARD = "punchcard"
    QUIZ = "quiz"
    POLL = "poll"
    MORE_ACTIVITIES = "more_activities"
    OTHER = "other"


class ActivityStatus(str, Enum):
    COMPLETED = "completed"
    IN_PROGRESS = "in_progress"
    AVAILABLE = "available"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ReviewActivity:
    """A normalized activity record for manual review only."""

    title: str
    kind: ActivityKind
    status: ActivityStatus
    points: int | None = None
    progress_done: int | None = None
    progress_total: int | None = None
    manual_url: str | None = None
    source_section: str | None = None
    description: str = ""
    action: str = "manual_review"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["kind"] = self.kind.value
        value["status"] = self.status.value
        return value


_SPACE_RE = re.compile(r"\s+")
_PROGRESS_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")
_POINTS_RE = re.compile(r"^\s*(\d+)\s*$")
_ALLOWED_HOSTS = ("rewards.bing.com", "bing.com", "msn.com", "microsoft.com")

# This script reads rendered card metadata only. There are deliberately no
# click(), submit(), send_keys(), window.open(), or location assignment calls.
_ACTIVITY_SCAN_JS = r"""
try {
  var out = [];
  var seen = {};
  var specs = [
    ["#dailyset a[href]", "Daily Set"],
    ["#moreactivities a[href]", "More Activities"],
    ['a[href*="/earn/quest/"]', "Punchcards"],
    ["mee-rewards-daily-set-item-content .rewards-card-container a[href]", "Daily Set"],
    ["mee-rewards-more-activities-card-item .rewards-card-container a[href]", "More Activities"],
    ["mee-rewards-more-activities-card .rewards-card-container a[href]", "More Activities"]
  ];
  function add(a, section, keywordOnly) {
    if (!a || !a.href) return;
    var card = a.closest('article, li, [role="listitem"], [class*="card"], [class*="Card"]') || a;
    var text = (card.innerText || card.textContent || a.innerText || a.textContent || "")
      .replace(/\s+/g, " ").trim().slice(0, 1200);
    var titleNode = a.querySelector('[class*="Strong"], h1, h2, h3, p')
      || card.querySelector('h1, h2, h3, [class*="Strong"], p');
    var title = (titleNode ? titleNode.textContent : (a.innerText || a.textContent || a.getAttribute("aria-label") || ""))
      .replace(/\s+/g, " ").trim().slice(0, 120);
    if (!title && !text) return;
    if (keywordOnly && !/read\s*(to|and)\s*earn|read\s+(the\s+)?(latest\s+)?(stories|news|articles?)|explore\s+(on|with)\s+bing|related\s+topics?|quiz|trivia|poll/i.test(title + " " + text)) return;
    var href = a.href || a.getAttribute("href") || "";
    var pointsMatch = text.match(/\+\s*(\d+)\s*(?:points?)?/i);
    var progressMatch = text.match(/\b(\d+)\s*\/\s*(\d+)\b/);
    var completed = !!card.querySelector('[class*="statusSuccess"], [aria-label*="complete" i]');
    var key = section + "|" + href + "|" + title;
    if (seen[key]) return;
    seen[key] = true;
    out.push({
      title: title || text.slice(0, 80),
      description: text,
      section: section,
      url: href,
      points: pointsMatch ? parseInt(pointsMatch[1], 10) : null,
      progress: progressMatch ? progressMatch[1] + "/" + progressMatch[2] : null,
      completed: completed ? true : null
    });
  }
  for (var s = 0; s < specs.length; s++) {
    var nodes = document.querySelectorAll(specs[s][0]);
    for (var i = 0; i < nodes.length; i++) add(nodes[i], specs[s][1], false);
  }
  // Reading/Explore cards may be rendered outside the common card sections.
  var allLinks = document.querySelectorAll("a[href]");
  for (var j = 0; j < allLinks.length; j++) add(allLinks[j], "Dashboard discovery", true);
  return out;
} catch (e) {
  return {error: String(e).slice(0, 180)};
}
"""


def _text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return _SPACE_RE.sub(" ", value).strip()


def _is_official_https_url(value: Any) -> str | None:
    candidate = _text(value)
    if not candidate:
        return None
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return None
    host = parsed.hostname.lower().rstrip(".")
    if not any(
        host == domain or host.endswith("." + domain) for domain in _ALLOWED_HOSTS
    ):
        return None
    if parsed.username or parsed.password:
        return None
    return candidate


def _parse_points(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    match = _POINTS_RE.fullmatch(_text(value))
    return int(match.group(1)) if match else None


def _parse_progress(value: Any) -> tuple[int | None, int | None]:
    if isinstance(value, Mapping):
        done = _parse_points(value.get("done"))
        total = _parse_points(value.get("total"))
    else:
        match = _PROGRESS_RE.fullmatch(_text(value))
        if not match:
            return None, None
        done, total = int(match.group(1)), int(match.group(2))
    if done is None or total is None or total <= 0 or done > total:
        return None, None
    return done, total


def _status(
    record: Mapping[str, Any],
    progress: tuple[int | None, int | None],
) -> ActivityStatus:
    completed = record.get("completed")
    if completed is True:
        return ActivityStatus.COMPLETED
    if completed is False:
        return ActivityStatus.AVAILABLE
    done, total = progress
    if done is not None and total is not None:
        if done >= total:
            return ActivityStatus.COMPLETED
        if done > 0:
            return ActivityStatus.IN_PROGRESS
        return ActivityStatus.AVAILABLE
    raw = _text(record.get("status")).casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "complete": ActivityStatus.COMPLETED,
        "completed": ActivityStatus.COMPLETED,
        "done": ActivityStatus.COMPLETED,
        "in_progress": ActivityStatus.IN_PROGRESS,
        "started": ActivityStatus.IN_PROGRESS,
        "available": ActivityStatus.AVAILABLE,
        "not_started": ActivityStatus.AVAILABLE,
    }
    return aliases.get(raw, ActivityStatus.UNKNOWN)


def _classify_text(
    title: str, description: str, section: str, url: str
) -> ActivityKind:
    combined = " ".join((title, description, section, url)).casefold()
    compact = re.sub(r"[^a-z0-9]+", " ", combined).strip()
    read_patterns = (
        r"\bread\s*(?:to|and)\s*earn\b"
        r"|\bread\s+articles?\s+to\s+earn\b"
        r"|\bread\s+(?:the\s+)?(?:latest\s+)?(?:stories|news|articles?)\b"
    )
    if re.search(read_patterns, compact):
        return ActivityKind.READ_TO_EARN
    explore_patterns = (
        r"\bexplore\s+(?:on|with)\s+bing\b"
        r"|\bexplore\s+(?:a\s+)?topic\s+(?:on\s+)?bing\b"
        r"|\brelated\s+topics?\b"
    )
    if re.search(explore_patterns, compact):
        return ActivityKind.EXPLORE
    if "/earn/quest/" in url.casefold() or re.search(
        r"\b(punch\s*card|quest)\b", compact
    ):
        return ActivityKind.PUNCHCARD
    if re.search(r"\b(daily\s+set|dailyset)\b", compact):
        return ActivityKind.DAILY_SET
    if re.search(r"\b(quiz|trivia|test\s+your\s+knowledge)\b", compact):
        return ActivityKind.QUIZ
    if re.search(r"\b(poll|vote\s+in|survey)\b", compact):
        return ActivityKind.POLL
    if re.search(r"\bmore\s+activities\b", compact):
        return ActivityKind.MORE_ACTIVITIES
    return ActivityKind.OTHER


def classify_activity(record: Mapping[str, Any]) -> ReviewActivity:
    """Classify a manually supplied or dashboard-read record."""
    if not isinstance(record, Mapping):
        raise TypeError("Each activity must be a JSON object.")
    title = _text(record.get("title"))
    if not title:
        raise ValueError("Each activity must contain a non-empty title.")
    description = _text(record.get("description") or record.get("details"))
    section = _text(record.get("section"))
    raw_url = record.get("url") or record.get("href")
    safe_url = _is_official_https_url(raw_url)
    progress = _parse_progress(record.get("progress"))
    return ReviewActivity(
        title=title,
        kind=_classify_text(title, description, section, _text(raw_url)),
        status=_status(record, progress),
        points=_parse_points(record.get("points")),
        progress_done=progress[0],
        progress_total=progress[1],
        manual_url=safe_url,
        source_section=section or None,
        description=description,
    )


def review_activities(records: Iterable[Mapping[str, Any]]) -> list[ReviewActivity]:
    """Normalize records and remove exact duplicates without opening anything."""
    if isinstance(records, (str, bytes, Mapping)):
        raise TypeError("Activities must be a list of JSON objects.")
    result: list[ReviewActivity] = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        activity = classify_activity(record)
        key = (activity.title.casefold(), (activity.manual_url or "").casefold())
        if key in seen:
            continue
        seen.add(key)
        result.append(activity)
    return result


def capture_dashboard_activities(
    driver, poll_attempts: int = 4
) -> list[ReviewActivity]:
    """Read visible activity cards from the dashboard and /earn page.

    Only the two dashboard pages are loaded. Activity links are extracted as
    manual handoffs but are never visited. The current tab and URL are restored
    before returning, including when one page fails to load.
    """
    if driver is None:
        raise ValueError("A live Rewards WebDriver is required.")
    if (
        isinstance(poll_attempts, bool)
        or not isinstance(poll_attempts, int)
        or not 1 <= poll_attempts <= 10
    ):
        raise ValueError("poll_attempts must be an integer from 1 to 10.")

    original_url = ""
    original_handle = None
    try:
        original_url = str(driver.current_url or "")
    except Exception:
        pass
    try:
        original_handle = driver.current_window_handle
    except Exception:
        pass

    records: list[Mapping[str, Any]] = []
    page_errors: list[str] = []
    try:
        for label, url in (("Dashboard", DASHBOARD_URL), ("Earn", EARN_URL)):
            try:
                driver.get(url)
                page_url = str(driver.current_url or url)
                page_host = urlparse(page_url).hostname
                if not page_host or not any(
                    page_host.lower() == domain
                    or page_host.lower().endswith("." + domain)
                    for domain in _ALLOWED_HOSTS
                ):
                    page_errors.append(
                        f"{label}: page redirected away from Microsoft Rewards; "
                        "check the signed-in Edge profile."
                    )
                    continue
                page_records = []
                for attempt in range(poll_attempts):
                    result = driver.execute_script(_ACTIVITY_SCAN_JS)
                    if isinstance(result, list):
                        page_records = result
                        if page_records:
                            break
                    elif isinstance(result, Mapping) and result.get("error"):
                        page_errors.append(f"{label}: {result['error']}")
                        break
                    if attempt + 1 < poll_attempts:
                        time.sleep(0.35)
                for row in page_records:
                    if isinstance(row, Mapping):
                        row = dict(row)
                        row.setdefault("section", label)
                        records.append(row)
            except Exception as exc:
                page_errors.append(f"{label}: {type(exc).__name__}: {exc}")
    finally:
        try:
            handles = list(driver.window_handles)
            if original_handle and original_handle in handles:
                driver.switch_to.window(original_handle)
            if original_url and str(driver.current_url or "") != original_url:
                driver.get(original_url)
        except Exception:
            # Scanning is best-effort; do not hide the original scan result.
            pass

    activities = review_activities(records)
    if page_errors and not activities:
        raise RuntimeError("; ".join(page_errors))
    return activities


def load_snapshot(path: str | Path) -> list[ReviewActivity]:
    """Load a user-created JSON snapshot from disk; performs no network access."""
    snapshot_path = Path(path)
    if snapshot_path.stat().st_size > 1_000_000:
        raise ValueError("Snapshot is larger than the 1 MB safety limit.")
    with snapshot_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    records = payload.get("activities") if isinstance(payload, Mapping) else payload
    if not isinstance(records, list):
        raise ValueError(
            "Snapshot must be a list or an object with an 'activities' list."
        )
    return review_activities(records)


def render_report(activities: Iterable[ReviewActivity]) -> str:
    """Render a concise Markdown manual-review list."""
    items = list(activities)
    if not items:
        return "No activities were supplied in the snapshot."
    lines = [
        "# Microsoft Rewards activity review",
        "",
        "This report only organizes dashboard information. Complete activities yourself.",
        "",
        "| Activity | Type | Status | Points | Progress | Manual link |",
        "|---|---|---|---:|---:|---|",
    ]
    for item in items:
        points = str(item.points) if item.points is not None else "—"
        progress = (
            f"{item.progress_done}/{item.progress_total}"
            if item.progress_done is not None and item.progress_total is not None
            else "—"
        )
        link = f"[Open manually]({item.manual_url})" if item.manual_url else "—"
        cells = [item.title, item.kind.value, item.status.value, points, progress, link]
        escaped_cells = [value.replace("|", "\\|") for value in cells]
        lines.append("| " + " | ".join(escaped_cells) + " |")
    lines.extend(
        [
            "",
            "No searches, article reading, quiz answers, poll responses, clicks,",
            "or point claims were performed by this tool.",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Classify a manually prepared Microsoft Rewards activity snapshot."
    )
    parser.add_argument("snapshot", help="Path to a JSON file you prepared manually.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print normalized JSON instead of Markdown.",
    )
    args = parser.parse_args(argv)
    try:
        activities = load_snapshot(args.snapshot)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    if args.json:
        payload = [item.to_dict() for item in activities]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(render_report(activities))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
