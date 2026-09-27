"""Unit tests for the reminder-reschedule resolution helpers.

`app/graph/nodes/_reminder_reschedule.py` decides which reminders a
time-only follow-up ("make it 6pm") may move and validates the label the
intake model returns. No LLM, no database; Notion is the in-memory fake.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.graph.nodes._reminder_reschedule import (
    RescheduleCandidate,
    load_reschedule_candidates,
    recent_reminder_entries,
    render_reschedule_candidates,
    resolve_reschedule_target,
)
from tests.support.notion_fake import FakeNotion

NOW = datetime(2026, 9, 27, 15, 0, tzinfo=UTC)


def _entry(
    page_id: str,
    title: str,
    *,
    kind: str = "reminder",
    event: str = "added",
    minutes_ago: float | None = 1,
) -> dict[str, object]:
    entry: dict[str, object] = {"page_id": page_id, "title": title, "kind": kind, "event": event}
    if minutes_ago is not None:
        entry["at"] = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    return entry


def test_recent_reminder_entries_keeps_fresh_titled_added_or_reminded_reminders() -> None:
    entries = [
        _entry("<page_task>", "Sort the mail", kind="task", event="added"),
        _entry("<page_done>", "Take the bins out", event="completed"),
        _entry("<page_untitled>", "", event="reminded"),
        _entry("<page_new>", "Call the pharmacy", event="added", minutes_ago=2),
        _entry("<page_sent>", "Water the plants", event="reminded", minutes_ago=90),
        _entry("<page_stale>", "Feed the cat", event="added", minutes_ago=25 * 60),
    ]
    assert recent_reminder_entries(entries, now=NOW) == [
        ("<page_new>", "Call the pharmacy"),
        ("<page_sent>", "Water the plants"),
    ]


def test_recent_reminder_entries_keeps_undated_and_drops_unparseable() -> None:
    entries = [
        _entry("<page_undated>", "Call the pharmacy", minutes_ago=None),
        {**_entry("<page_bad>", "Water the plants"), "at": "not a time"},
    ]
    assert recent_reminder_entries(entries, now=NOW) == [("<page_undated>", "Call the pharmacy")]


def test_recent_reminder_entries_caps_and_ignores_junk() -> None:
    entries: list[object] = ["junk", None, 42]
    entries += [_entry(f"<page_{n}>", f"Reminder {n}") for n in range(5)]
    picked = recent_reminder_entries(entries, now=NOW)
    assert [page for page, _ in picked] == ["<page_0>", "<page_1>", "<page_2>"]


def test_resolve_reschedule_target_accepts_only_shown_labels() -> None:
    candidates = [
        RescheduleCandidate("R1", "<page_a>", "Call the pharmacy", None, "Pending"),
        RescheduleCandidate("R2", "<page_b>", "Water the plants", None, "Completed"),
    ]
    assert resolve_reschedule_target(" r2 ", candidates) == candidates[1]
    assert resolve_reschedule_target("R1", candidates) == candidates[0]
    for bad in (None, "", "R3", "<page_a>", "Call the pharmacy", 1, {"label": "R1"}):
        assert resolve_reschedule_target(bad, candidates) is None
    assert resolve_reschedule_target("R1", []) is None


def test_render_reschedule_candidates_shows_label_title_and_local_time_never_ids() -> None:
    candidates = [
        RescheduleCandidate(
            "R1",
            "<page_a>",
            "Call\nthe pharmacy",
            datetime(2026, 10, 1, 22, 0, tzinfo=UTC),
            "Pending",
        ),
        RescheduleCandidate("R2", "<page_b>", "Water the plants", None, "Pending"),
    ]
    rendered = render_reschedule_candidates(candidates, user_timezone="America/Chicago")
    lines = rendered.splitlines()
    assert lines[0] == (
        '- R1: "Call the pharmacy" — set for Thu 2026-10-01 17:00 (2026-10-01T17:00:00-05:00)'
    )
    assert lines[1] == '- R2: "Water the plants" — time unknown'
    assert "<page_" not in rendered
    assert render_reschedule_candidates([], user_timezone="America/Chicago") == "None."


def test_render_reschedule_candidates_survives_an_unknown_timezone() -> None:
    candidates = [
        RescheduleCandidate(
            "R1",
            "<page_a>",
            "Call the pharmacy",
            datetime(2026, 10, 1, 22, 0, tzinfo=UTC),
            "Pending",
        )
    ]
    rendered = render_reschedule_candidates(candidates, user_timezone="Not/AZone")
    assert "2026-10-01T22:00:00+00:00" in rendered


@pytest.mark.asyncio
async def test_load_reschedule_candidates_confirms_reminder_pages_in_notion() -> None:
    fake = FakeNotion()
    reminder = fake.seed_task(
        title="Call the pharmacy",
        is_reminder=True,
        remind_at="2026-10-01T17:00:00-05:00",
        status="Completed",
    )
    plain = fake.seed_task(title="Water the plants", is_reminder=False)
    undo = fake.install()
    try:
        candidates = await load_reschedule_candidates(
            [
                _entry(plain, "Water the plants"),
                _entry(reminder, "Call the pharmacy", event="reminded"),
                _entry("<page_missing>", "Feed the cat"),
            ],
            now=NOW,
        )
    finally:
        undo()
    # The non-reminder page and the page Notion cannot read are dropped; the
    # survivor is labelled R1.
    assert candidates == [
        RescheduleCandidate(
            "R1",
            reminder,
            "Call the pharmacy",
            datetime(2026, 10, 1, 22, 0, tzinfo=UTC),
            "Completed",
        )
    ]


@pytest.mark.asyncio
async def test_load_reschedule_candidates_makes_no_notion_call_without_a_ledger_reminder() -> None:
    get_page = AsyncMock()
    with patch("app.tools.notion.get_page", get_page):
        assert await load_reschedule_candidates([], now=NOW) == []
        assert (
            await load_reschedule_candidates(
                [_entry("<page_task>", "Sort the mail", kind="task")], now=NOW
            )
            == []
        )
    get_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_load_reschedule_candidates_drops_a_timed_out_lookup(monkeypatch) -> None:
    import app.graph.nodes._reminder_reschedule as mod

    async def _slow(_page_id: str) -> dict:
        await asyncio.sleep(1)
        return {}

    monkeypatch.setattr(mod, "CANDIDATE_LOOKUP_TIMEOUT_SECONDS", 0.01)
    with patch("app.tools.notion.get_page", _slow):
        assert (
            await load_reschedule_candidates([_entry("<page_a>", "Call the pharmacy")], now=NOW)
            == []
        )
