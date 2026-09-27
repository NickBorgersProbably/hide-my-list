"""Unit tests for CANNOT_FINISH's task resolution and sub-task parsing.

`ledger_anchor` picks the task "I can't finish that today" refers to when no
task is active; `remaining_sub_tasks` turns the model's breakdown into the
sub-task rows the node writes. Pure functions: no LLM, no Notion.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.graph.nodes.cannot_finish import ledger_anchor, remaining_sub_tasks

NOW = datetime(2026, 9, 27, 15, 0, tzinfo=UTC)


def _entry(page_id: str, title: str, event: str, *, hours_ago: float | None = 0.1,
           kind: str = "task") -> dict[str, object]:
    entry: dict[str, object] = {"page_id": page_id, "title": title, "kind": kind, "event": event}
    if hours_ago is not None:
        entry["at"] = (NOW - timedelta(hours=hours_ago)).isoformat()
    return entry


def test_ledger_anchor_takes_the_newest_nudged_reminded_or_suggested_entry() -> None:
    entries = [
        _entry("<page_done>", "Sort the mail", "completed"),
        _entry("<page_added>", "Book the placeholder appointment", "added"),
        _entry("<page_nudged>", "Renew the car registration", "nudged", hours_ago=0.5),
        _entry("<page_suggested>", "Water the plants", "suggested", hours_ago=1),
    ]
    assert ledger_anchor(entries, now=NOW) == ("<page_nudged>", "Renew the car registration")


def test_ledger_anchor_accepts_reminded_and_suggested() -> None:
    assert ledger_anchor(
        [_entry("<page_r>", "Call the pharmacy", "reminded", kind="reminder")], now=NOW
    ) == ("<page_r>", "Call the pharmacy")
    assert ledger_anchor(
        [_entry("<page_s>", " Water the plants ", "suggested")], now=NOW
    ) == ("<page_s>", "Water the plants")


def test_ledger_anchor_skips_untitled_stale_and_malformed_entries() -> None:
    entries: list[object] = [
        "junk",
        {"event": "nudged", "title": "No page id"},
        _entry("<page_untitled>", "", "nudged"),
        _entry("<page_stale>", "Renew the car registration", "nudged", hours_ago=25),
        {**_entry("<page_bad_at>", "Water the plants", "nudged"), "at": "yesterday-ish"},
        {**_entry("<page_num_at>", "Water the plants", "nudged"), "at": 12},
    ]
    assert ledger_anchor(entries, now=NOW) is None
    assert ledger_anchor(None, now=NOW) is None


def test_ledger_anchor_keeps_an_undated_entry() -> None:
    entries = [_entry("<page_n>", "Renew the car registration", "nudged", hours_ago=None)]
    assert ledger_anchor(entries, now=NOW) == ("<page_n>", "Renew the car registration")


def test_remaining_sub_tasks_only_for_analyze_remaining() -> None:
    assert remaining_sub_tasks(None) == []
    assert remaining_sub_tasks({"phase": "ask_progress", "progress_question": "?"}) == []
    assert remaining_sub_tasks({"phase": "analyze_remaining"}) == []
    assert remaining_sub_tasks(
        {"phase": "analyze_remaining", "remaining_sub_tasks": "fill the form"}
    ) == []


def test_remaining_sub_tasks_cleans_each_entry() -> None:
    parsed = {
        "phase": "analyze_remaining",
        "remaining_sub_tasks": [
            {"title": "  Fill in\nthe form ", "time_estimate_minutes": 20, "sequence": 1},
            {"title": "", "time_estimate_minutes": 10, "sequence": 2},
            "junk",
            {"title": "Mail it", "time_estimate_minutes": 0, "sequence": 0},
            {"title": "Pay the fee", "time_estimate_minutes": True, "sequence": "3"},
            {"title": "x" * 300},
        ],
    }
    assert remaining_sub_tasks(parsed) == [
        {"title": "Fill in the form", "time_estimate_minutes": 20, "sequence": 1},
        {"title": "Mail it", "time_estimate_minutes": 30, "sequence": 2},
        {"title": "Pay the fee", "time_estimate_minutes": 30, "sequence": 3},
        {"title": "x" * 200, "time_estimate_minutes": 30, "sequence": 4},
    ]


def test_remaining_sub_tasks_caps_the_list() -> None:
    parsed = {
        "phase": "analyze_remaining",
        "remaining_sub_tasks": [{"title": f"Step {n}"} for n in range(10)],
    }
    cleaned = remaining_sub_tasks(parsed)
    assert [sub["title"] for sub in cleaned] == [f"Step {n}" for n in range(6)]
