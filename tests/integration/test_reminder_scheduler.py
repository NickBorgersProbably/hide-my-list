"""Integration tests for the reminder_scheduler daemon (backstop path).

Tests the run_reminder_scheduler() daemon with mocked Notion and mocked
schedule_for_task (to isolate daemon logic from scheduling algorithm).
DB-backed tests use real Postgres and are skipped when DATABASE_URL is absent.

Covers:
- Empty Notion response → no writes
- One task with deadline → schedule_for_task called; mark_reminder_scheduled called on success
- Rerun on already-scheduled task → no-op (Notion filter excludes it)
- Deadline changed in Notion → old ledger rows superseded, old outbox rows dead, new rows scheduled
- Planner fallback (enqueue_failures) → ops_alert emitted; mark_reminder_scheduled NOT called
- Notion 5xx → ops_alert emitted; cycle aborted

Private data: all values are placeholders / synthetic.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_notion_task(
    page_id: str,
    title: str = "Placeholder task",
    due_at_iso: str = "2026-06-05T17:00:00-05:00",
    urgency: int = 50,
) -> dict:
    return {
        "id": page_id,
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Status": {"select": {"name": "Pending"}},
            "Due At": {"date": {"start": due_at_iso}},
            "Urgency": {"number": urgency},
        },
    }


def _notion_result(tasks: list[dict]) -> dict:
    return {"results": tasks, "has_more": False}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_notion_no_writes() -> None:
    """Empty Notion result → no schedule_for_task calls and no mark_reminder_scheduled calls."""
    mark_calls: list = []
    schedule_calls: list = []

    async def mock_mark_scheduled(pid: str) -> dict:
        mark_calls.append(pid)
        return {}

    async def mock_schedule(**kwargs: Any) -> tuple:
        schedule_calls.append(kwargs)
        return ([], [])

    with (
        patch(
            "app.tools.notion.query_tasks_with_unscheduled_deadlines",
            new_callable=AsyncMock,
            return_value=_notion_result([]),
        ),
        patch("app.tools.notion.mark_reminder_scheduled", side_effect=mock_mark_scheduled),
        patch("app.scheduler.reminder_scheduling.schedule_for_task", side_effect=mock_schedule),
        patch("app.scheduler.reminder_scheduling.get_active_deadline_for_page", new_callable=AsyncMock, return_value=None),
    ):
        from app.scheduler.reminder_scheduler import run_reminder_scheduler
        await run_reminder_scheduler()

    assert len(mark_calls) == 0
    assert len(schedule_calls) == 0


@pytest.mark.asyncio
async def test_one_task_schedules_and_marks(monkeypatch: pytest.MonkeyPatch) -> None:
    """One orphan task → schedule_for_task called; mark_reminder_scheduled called on success."""
    monkeypatch.setenv("AUTHORIZED_PEERS", "<test-peer-number>")

    page_id = str(uuid.uuid4())
    task = _make_notion_task(page_id=page_id, due_at_iso="2026-06-05T17:00:00-05:00", urgency=60)

    mark_calls: list = []
    schedule_calls: list = []

    fake_slots = [("3d", datetime(2026, 6, 2, 14, 0, tzinfo=UTC))]

    async def mock_mark_scheduled(pid: str) -> dict:
        mark_calls.append(pid)
        return {}

    async def mock_schedule(**kwargs: Any) -> tuple:
        schedule_calls.append(kwargs)
        return (fake_slots, [])

    with (
        patch(
            "app.tools.notion.query_tasks_with_unscheduled_deadlines",
            new_callable=AsyncMock,
            return_value=_notion_result([task]),
        ),
        patch("app.tools.notion.mark_reminder_scheduled", side_effect=mock_mark_scheduled),
        patch("app.scheduler.reminder_scheduling.schedule_for_task", side_effect=mock_schedule),
        patch("app.scheduler.reminder_scheduling.get_active_deadline_for_page", new_callable=AsyncMock, return_value=None),
    ):
        from app.scheduler.reminder_scheduler import run_reminder_scheduler
        await run_reminder_scheduler()

    assert len(schedule_calls) == 1
    assert schedule_calls[0]["page_id"] == page_id
    assert len(mark_calls) == 1
    assert mark_calls[0] == page_id


@pytest.mark.asyncio
async def test_already_scheduled_task_skipped() -> None:
    """Tasks already scheduled (Reminder Scheduled At set) are excluded by Notion filter.

    The Notion query only returns tasks where Reminder Scheduled At is empty.
    So an already-scheduled task never appears — this tests the empty-result path.
    """
    # An already-scheduled task would not appear in query_tasks_with_unscheduled_deadlines
    # because Notion filters to Reminder Scheduled At is_empty.
    schedule_calls: list = []

    async def mock_schedule(**kwargs: Any) -> tuple:
        schedule_calls.append(kwargs)
        return ([], [])

    with (
        patch(
            "app.tools.notion.query_tasks_with_unscheduled_deadlines",
            new_callable=AsyncMock,
            return_value=_notion_result([]),  # Empty — already-scheduled tasks excluded
        ),
        patch("app.scheduler.reminder_scheduling.schedule_for_task", side_effect=mock_schedule),
        patch("app.scheduler.reminder_scheduling.get_active_deadline_for_page", new_callable=AsyncMock, return_value=None),
    ):
        from app.scheduler.reminder_scheduler import run_reminder_scheduler
        await run_reminder_scheduler()

    # Never called — no tasks to process
    assert len(schedule_calls) == 0


@pytest.mark.asyncio
async def test_deadline_changed_supersedes_old_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deadline changed in Notion → supersede_ledger_rows + cancel_outbox_rows called."""
    monkeypatch.setenv("AUTHORIZED_PEERS", "<test-peer-number>")

    page_id = str(uuid.uuid4())
    new_due = "2026-06-10T17:00:00-05:00"
    old_deadline = datetime(2026, 6, 5, 22, 0, tzinfo=UTC)  # Old: Jun 5 UTC

    task = _make_notion_task(page_id=page_id, due_at_iso=new_due, urgency=60)

    supersede_calls: list = []
    cancel_calls: list = []
    schedule_calls: list = []

    old_outbox_ids = [uuid.uuid4()]

    async def mock_supersede(pid: str) -> list:
        supersede_calls.append(pid)
        return old_outbox_ids

    async def mock_cancel(oids: list) -> None:
        cancel_calls.append(oids)

    fake_slots = [("3d", datetime(2026, 6, 7, 14, 0, tzinfo=UTC))]

    async def mock_schedule(**kwargs: Any) -> tuple:
        schedule_calls.append(kwargs)
        return (fake_slots, [])

    with (
        patch(
            "app.tools.notion.query_tasks_with_unscheduled_deadlines",
            new_callable=AsyncMock,
            return_value=_notion_result([task]),
        ),
        patch("app.tools.notion.mark_reminder_scheduled", new_callable=AsyncMock, return_value={}),
        patch(
            "app.scheduler.reminder_scheduling.get_active_deadline_for_page",
            new_callable=AsyncMock,
            return_value=old_deadline,  # Prior deadline exists and differs
        ),
        patch("app.scheduler.reminder_scheduling.supersede_ledger_rows", side_effect=mock_supersede),
        patch("app.scheduler.reminder_scheduling.cancel_outbox_rows", side_effect=mock_cancel),
        patch("app.scheduler.reminder_scheduling.schedule_for_task", side_effect=mock_schedule),
    ):
        from app.scheduler.reminder_scheduler import run_reminder_scheduler
        await run_reminder_scheduler()

    # Old rows were superseded and outbox rows cancelled
    assert len(supersede_calls) == 1
    assert supersede_calls[0] == page_id
    assert len(cancel_calls) == 1
    assert cancel_calls[0] == old_outbox_ids

    # New schedule was created
    assert len(schedule_calls) == 1


@pytest.mark.asyncio
async def test_enqueue_failure_emits_ops_alert_no_mark_scheduled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Partial enqueue failure → ops_alert emitted; mark_reminder_scheduled NOT called."""
    monkeypatch.setenv("AUTHORIZED_PEERS", "<test-peer-number>")

    page_id = str(uuid.uuid4())
    task = _make_notion_task(page_id=page_id, due_at_iso="2026-06-05T17:00:00-05:00", urgency=60)

    ops_alert_calls: list = []
    mark_calls: list = []

    async def mock_ops_enqueue(kind: str, body: str, severity: str = "warning") -> uuid.UUID:
        ops_alert_calls.append({"kind": kind, "body": body})
        return uuid.uuid4()

    async def mock_mark_scheduled(pid: str) -> dict:
        mark_calls.append(pid)
        return {}

    # schedule_for_task returns failure
    async def mock_schedule(**kwargs: Any) -> tuple:
        return ([], ["4h"])

    with (
        patch(
            "app.tools.notion.query_tasks_with_unscheduled_deadlines",
            new_callable=AsyncMock,
            return_value=_notion_result([task]),
        ),
        patch("app.tools.notion.mark_reminder_scheduled", side_effect=mock_mark_scheduled),
        patch("app.tools.ops_alerts.enqueue", side_effect=mock_ops_enqueue),
        patch("app.scheduler.reminder_scheduling.schedule_for_task", side_effect=mock_schedule),
        patch("app.scheduler.reminder_scheduling.get_active_deadline_for_page", new_callable=AsyncMock, return_value=None),
    ):
        from app.scheduler.reminder_scheduler import run_reminder_scheduler
        await run_reminder_scheduler()

    # ops_alert was emitted
    assert len(ops_alert_calls) >= 1
    assert any("failure" in c["kind"] or "partial" in c["kind"] for c in ops_alert_calls)

    # mark_reminder_scheduled was NOT called
    assert len(mark_calls) == 0


@pytest.mark.asyncio
async def test_notion_5xx_emits_ops_alert_and_aborts() -> None:
    """Notion 5xx on query → ops_alert emitted; cycle aborts (no schedule_for_task calls)."""
    ops_alert_calls: list = []
    schedule_calls: list = []

    async def mock_ops_enqueue(kind: str, body: str, severity: str = "warning") -> uuid.UUID:
        ops_alert_calls.append({"kind": kind})
        return uuid.uuid4()

    async def mock_schedule(**kwargs: Any) -> tuple:
        schedule_calls.append(kwargs)
        return ([], [])

    with (
        patch(
            "app.tools.notion.query_tasks_with_unscheduled_deadlines",
            new_callable=AsyncMock,
            side_effect=Exception("HTTP 503 Service Unavailable"),
        ),
        patch("app.tools.ops_alerts.enqueue", side_effect=mock_ops_enqueue),
        patch("app.scheduler.reminder_scheduling.schedule_for_task", side_effect=mock_schedule),
    ):
        from app.scheduler.reminder_scheduler import run_reminder_scheduler
        await run_reminder_scheduler()

    # ops_alert emitted
    assert len(ops_alert_calls) >= 1
    assert any("notion" in c["kind"] for c in ops_alert_calls)

    # No scheduling attempted
    assert len(schedule_calls) == 0


@pytest.mark.asyncio
async def test_task_missing_deadline_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task in Notion response with no Due At → skipped gracefully."""
    monkeypatch.setenv("AUTHORIZED_PEERS", "<test-peer-number>")

    page_id = str(uuid.uuid4())
    bad_task = {
        "id": page_id,
        "properties": {
            "Title": {"title": [{"plain_text": "Placeholder task no due"}]},
            "Status": {"select": {"name": "Pending"}},
            "Due At": {"date": None},  # No date
            "Urgency": {"number": 50},
        },
    }

    schedule_calls: list = []

    async def mock_schedule(**kwargs: Any) -> tuple:
        schedule_calls.append(kwargs)
        return ([], [])

    with (
        patch(
            "app.tools.notion.query_tasks_with_unscheduled_deadlines",
            new_callable=AsyncMock,
            return_value=_notion_result([bad_task]),
        ),
        patch("app.scheduler.reminder_scheduling.schedule_for_task", side_effect=mock_schedule),
        patch("app.scheduler.reminder_scheduling.get_active_deadline_for_page", new_callable=AsyncMock, return_value=None),
    ):
        from app.scheduler.reminder_scheduler import run_reminder_scheduler
        await run_reminder_scheduler()

    # Skipped — no schedule_for_task calls
    assert len(schedule_calls) == 0
