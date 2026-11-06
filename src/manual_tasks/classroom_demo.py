"""Offline classroom task plan for the sequential account demonstration.

These entries are local demonstration steps only. They never navigate to
Microsoft Rewards, operate a browser, submit a task, or award Rewards points.
"""

from __future__ import annotations

from typing import TypedDict


class ClassroomDemoTask(TypedDict):
    id: str
    title: str
    category: str


_CLASSROOM_DEMO_TASKS: tuple[ClassroomDemoTask, ...] = (
    {"id": "daily_check_in", "title": "Daily Check-in", "category": "daily_check_in"},
    {"id": "daily_set", "title": "Daily Set", "category": "daily_set"},
    {"id": "read_to_earn", "title": "Read to Earn", "category": "read_to_earn"},
    {"id": "explore_on_bing", "title": "Explore on Bing", "category": "explore"},
    {"id": "quiz", "title": "Quiz", "category": "quiz"},
    {"id": "poll", "title": "Poll", "category": "poll"},
    {"id": "punchcard", "title": "Punchcard / Quest", "category": "punchcard"},
    {"id": "more_activities", "title": "More Activities", "category": "more_activities"},
    {"id": "visual_search", "title": "Visual Search", "category": "visual_search"},
    {"id": "pc_searches", "title": "PC search task", "category": "pc_search"},
    {"id": "mobile_searches", "title": "Mobile search task", "category": "mobile_search"},
)


def get_classroom_demo_tasks() -> list[ClassroomDemoTask]:
    """Return a fresh ordered copy of every classroom-only demo task."""
    return [dict(task) for task in _CLASSROOM_DEMO_TASKS]
