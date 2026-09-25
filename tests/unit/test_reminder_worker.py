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
    """Deadline rows point at task pages, so delivery must not complete them."""
    from app.scheduler import reminder_worker
    from app.tools import notion

    reminder_id = uuid.uuid4()
    row = {
        "id": reminder_id,
        "peer": "<peer>",
        "body": "Deadline check-in.",
        "notion_page_id": "<page-id>",
        "idempotency_key": str(uuid.uuid4()),
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
    kwargs = signal_send.await_args.kwargs
    assert kwargs["recipient"] == "<peer>"
    assert kwargs["message"] == "Deadline check-in."
    assert kwargs["idempotency_key"] == row["idempotency_key"]
    complete_reminder.assert_not_awaited()

    # reminder_type='deadline' must cross the recent_outbound INSERT so
    # hydrate_context can classify the delivery as nudged, not reminded.
    insert_params = [
        params
        for sql, params in conn.executed
        if params is not None and "INSERT INTO recent_outbound" in sql
    ]
    assert insert_params, "Expected a recent_outbound INSERT for the deadline delivery"
    assert insert_params[0][3] == "deadline", (
        f"Expected reminder_type='deadline' in recent_outbound INSERT, got {insert_params[0][3]!r}"
    )


@pytest.mark.asyncio
async def test_dispatch_treats_missing_kind_as_reminder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows missing the field at mapping level keep legacy completion behavior."""
    from app.scheduler import reminder_worker
    from app.tools import notion

    reminder_id = uuid.uuid4()
    row = {
        "id": reminder_id,
        "peer": "<peer>",
        "body": "Legacy reminder",
        "notion_page_id": "<page-id>",
        "idempotency_key": str(uuid.uuid4()),
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

    complete_reminder.assert_awaited_once_with("<page-id>", "sent")


@pytest.mark.parametrize("kind", ["reminder", "deadline"])
@pytest.mark.asyncio
async def test_dispatch_records_the_row_kind_as_reminder_type(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """recent_outbound.reminder_type is the outbox row's kind, never a constant.

    A deadline nudge recorded as 'reminder' reads downstream as a reminder page
    the worker already completed, so a "done" after the nudge skipped the
    task's Completed write.
    """
    from app.scheduler import reminder_worker
    from app.tools import notion

    row = {
        "id": uuid.uuid4(),
        "peer": "<peer>",
        "body": "Test reminder",
        "notion_page_id": "<page-id>",
        "idempotency_key": str(uuid.uuid4()),
        "attempt": 0,
        "due_at": datetime.now(UTC) - timedelta(seconds=1),
        "kind": kind,
    }

    async def fake_claim_due_reminders(conn: Any, worker_id: str) -> list[dict[str, Any]]:
        return [row]

    monkeypatch.setattr(reminder_worker, "_claim_due_reminders", fake_claim_due_reminders)
    monkeypatch.setattr(notion, "complete_reminder", AsyncMock(return_value={}))

    conn = FakeConnection()
    await reminder_worker.dispatch_due_reminders(
        conn, signal_send_fn=AsyncMock(return_value={"timestamp": 12345})
    )

    inserts = [
        params for query, params in conn.executed if "INSERT INTO recent_outbound" in query
    ]
    assert len(inserts) == 1
    assert inserts[0] is not None
    # (peer, signal_timestamp, notion_page_id, reminder_type, title)
    assert inserts[0][:4] == ("<peer>", 12345, "<page-id>", kind)


def test_deadline_body_names_the_task() -> None:
    """The nudge names its task; a page with no readable title stays generic."""
    from app.scheduler.reminder_scheduling import _deadline_body

    assert _deadline_body("3d", title="  Placeholder task ") == (
        "Deadline nudge: Placeholder task. Want one tiny next step?"
    )
    assert _deadline_body("3d", title="") == (
        "Deadline nudge for this task. Want one tiny next step?"
    )
