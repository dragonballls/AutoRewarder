"""Offline classifier for a user-supplied Microsoft Rewards activity snapshot.

This module deliberately does not use Selenium, HTTP requests, browser APIs,
account cookies, or any Rewards endpoints. The user supplies a JSON snapshot
that they prepared manually from activities visible in their own dashboard.
The module classifies and organizes those records for a person to review.

It never opens links, submits searches, reads articles, answers quizzes/polls,
claims points, or changes account state.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse


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
        """Return a JSON-friendly representation with enum values as strings."""
        value = asdict(self)
        value["kind"] = self.kind.value
        value["status"] = self.status.value
        return value


_SPACE_RE = re.compile(r"\s+")
_PROGRESS_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")
_POINTS_RE = re.compile(r"^\s*(\d+)\s*$")

# Links are displayed for the user; this module does not navigate to them.
_ALLOWED_HOSTS = ("rewards.bing.com", "bing.com", "msn.com", "microsoft.com")


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
    if not any(host == domain or host.endswith("." + domain) for domain in _ALLOWED_HOSTS):
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


def _status(record: Mapping[str, Any], progress: tuple[int | None, int | None]) -> ActivityStatus:
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


def _classify_text(title: str, description: str, section: str, url: str) -> ActivityKind:
    combined = " ".join((title, description, section, url)).casefold()
    compact = re.sub(r"[^a-z0-9]+", " ", combined).strip()

    # Specific names precede broad category words to reduce false positives.
    if re.search(r"\bread\s*(?:to|and)\s*earn\b|\bread\s+articles?\s+to\s+earn\b", compact):
        return ActivityKind.READ_TO_EARN
    if re.search(r"\bexplore\s+(?:on|with)\s+bing\b|\bexplore\s+(?:a\s+)?topic\s+(?:on\s+)?bing\b|\brelated\s+topics?\b", compact):
        return ActivityKind.EXPLORE
    if "/earn/quest/" in url.casefold() or re.search(r"\b(punch\s*card|quest)\b", compact):
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
    """Classify one manually supplied dashboard record.

    Required field: title. Optional fields: description, section, url or href,
    points, progress ("1/3" or a mapping), completed, and status.
    """
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


def load_snapshot(path: str | Path) -> list[ReviewActivity]:
    """Load a user-created JSON snapshot from disk; performs no network access."""
    snapshot_path = Path(path)
    if snapshot_path.stat().st_size > 1_000_000:
        raise ValueError("Snapshot is larger than the 1 MB safety limit.")
    with snapshot_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    records = payload.get("activities") if isinstance(payload, Mapping) else payload
    if not isinstance(records, list):
        raise ValueError("Snapshot must be a list or an object with an 'activities' list.")
    return review_activities(records)


def render_report(activities: Iterable[ReviewActivity]) -> str:
    """Render a concise Markdown manual-review list."""
    items = list(activities)
    if not items:
        return "No activities were supplied in the snapshot."

    lines = [
        "# Microsoft Rewards activity review",
        "",
        "This report only organizes the snapshot you supplied. Complete activities yourself.",
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
        lines.append("| " + " | ".join(value.replace("|", "\\|") for value in cells) + " |")
    lines.extend(
        [
            "",
            "No searches, article reading, quiz answers, poll responses, clicks, or point claims were performed by this tool.",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Classify a manually prepared Microsoft Rewards activity snapshot."
    )
    parser.add_argument("snapshot", help="Path to a JSON file you prepared manually.")
    parser.add_argument("--json", action="store_true", help="Print normalized JSON instead of Markdown.")
    args = parser.parse_args(argv)
    try:
        activities = load_snapshot(args.snapshot)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    if args.json:
        print(json.dumps([item.to_dict() for item in activities], indent=2, ensure_ascii=False))
    else:
        print(render_report(activities))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
