"""Unit tests for the reminder delivery worker."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest


class FakeConnection:
    """Minimal async connection surface used by dispatch_due_reminders."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def execute(
        self,
        query: str,
        params: tuple[Any, ...] | None = None,
    ) -> None:
        self.executed.append((query, params))


@pytest.mark.asyncio
async def test_dispatch_due_reminders_accepts_native_uuid_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """psycopg maps Postgres UUID columns to uuid.UUID objects."""
    from app.scheduler import reminder_worker
    from app.tools import notion

    reminder_id = uuid.uuid4()
    idempotency_key = str(uuid.uuid4())
    row = {
        "id": reminder_id,
        "peer": "<peer>",
        "body": "Test reminder",
        "notion_page_id": "<page-id>",
        "idempotency_key": idempotency_key,
        "attempt": 0,
        "due_at": datetime.now(UTC) - timedelta(seconds=1),
    }

    async def fake_claim_due_reminders(
        conn: Any,
        worker_id: str,
    ) -> list[dict[str, Any]]:
        return [row]

    monkeypatch.setattr(reminder_worker, "_claim_due_reminders", fake_claim_due_reminders)
    complete_reminder = AsyncMock(return_value={})
    monkeypatch.setattr(notion, "complete_reminder", complete_reminder)

    signal_send = AsyncMock(return_value={"timestamp": 12345})
    conn = FakeConnection()

    await reminder_worker.dispatch_due_reminders(conn, signal_send_fn=signal_send)

    signal_send.assert_awaited_once_with(
        recipient="<peer>",
        message="Test reminder",
        idempotency_key=idempotency_key,
    )
    complete_reminder.assert_awaited_once_with("<page-id>", "sent")
    assert conn.rollbacks == 0
    assert any(params is not None and params[-1] == str(reminder_id) for _, params in conn.executed)


@pytest.mark.asyncio
async def test_dispatch_skips_complete_reminder_for_deadline_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CRITICAL: rows with kind='deadline' must NOT trigger
    notion.complete_reminder. Without this branch, the worker would silently
    mark the user's task as Completed on the first deadline ping (the page
    id is the user's task page id, not a dedicated reminder page).

    This guards the d-001 design collision from closed PR #601.
    """
    from app.scheduler import reminder_worker
    from app.tools import notion

    reminder_id = uuid.uuid4()
    idempotency_key = "deadline-<page-id>-3d-2026-06-01T17:00:00+00:00"
    row = {
        "id": reminder_id,
        "peer": "<peer>",
        "body": "Task placeholder is coming up in 3 days - want to start now?",
        "notion_page_id": "<page-id>",
        "idempotency_key": idempotency_key,
        "attempt": 0,
        "due_at": datetime.now(UTC) - timedelta(seconds=1),
        "kind": "deadline",
    }

    async def fake_claim_due_reminders(
        conn: Any,
        worker_id: str,
    ) -> list[dict[str, Any]]:
        return [row]

    monkeypatch.setattr(reminder_worker, "_claim_due_reminders", fake_claim_due_reminders)
    complete_reminder = AsyncMock(return_value={})
    monkeypatch.setattr(notion, "complete_reminder", complete_reminder)

    signal_send = AsyncMock(return_value={"timestamp": 12345})
    conn = FakeConnection()

    await reminder_worker.dispatch_due_reminders(conn, signal_send_fn=signal_send)

    signal_send.assert_awaited_once()
    # The whole point of the test: complete_reminder must NOT be called for
    # a deadline-kind row, because notion_page_id is the user's task page id
    # and complete_reminder would set Status=Completed.
    complete_reminder.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_treats_missing_kind_as_reminder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy rows that pre-date migration 0008 may surface with no `kind`
    field on the row dict; the worker must default to 'reminder' to preserve
    existing wall-clock completion behavior."""
    from app.scheduler import reminder_worker
    from app.tools import notion

    reminder_id = uuid.uuid4()
    row = {
        "id": reminder_id,
        "peer": "<peer>",
        "body": "Legacy reminder",
        "notion_page_id": "<legacy-page>",
        "idempotency_key": str(uuid.uuid4()),
        "attempt": 0,
        "due_at": datetime.now(UTC) - timedelta(seconds=1),
        # No `kind` key — simulates a row missing the column at the dict
        # mapping level.
    }

    async def fake_claim_due_reminders(
        conn: Any,
        worker_id: str,
    ) -> list[dict[str, Any]]:
        return [row]

    monkeypatch.setattr(reminder_worker, "_claim_due_reminders", fake_claim_due_reminders)
    complete_reminder = AsyncMock(return_value={})
    monkeypatch.setattr(notion, "complete_reminder", complete_reminder)

    signal_send = AsyncMock(return_value={"timestamp": 12345})
    conn = FakeConnection()

    await reminder_worker.dispatch_due_reminders(conn, signal_send_fn=signal_send)

    # Default behavior is preserved — legacy rows complete the task.
    complete_reminder.assert_awaited_once_with("<legacy-page>", "sent")
