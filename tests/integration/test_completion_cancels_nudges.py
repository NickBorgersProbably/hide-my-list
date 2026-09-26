"""Completing a task stops its deadline nudges.

`reminders.cancel_pending_nudges` marks the page's undelivered
`kind='deadline'` outbox rows dead and retires the series' ledger rows. Every
completion write path calls it: `complete_node`, the shared `_log_finished`
path (when it completes an open task), and the interaction review's
`complete_task` correction. Each call site swallows a failure (the worker's
pre-send check is the backstop), so each one's kwargs are pinned against the
real signature (clause 10).

The Postgres round-trip tests need DATABASE_URL; the call-site tests do not.
"""
from __future__ import annotations

import inspect
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from structlog.testing import capture_logs

from app.graph.state import State

_HAS_DB = bool(os.environ.get("DATABASE_URL", ""))
_REWARD = {"text": "Nice work!", "attachment_path": None}


@pytest.fixture()
async def db_conn() -> Any:
    import psycopg

    conn_str = os.environ["DATABASE_URL"]
    async with await psycopg.AsyncConnection.connect(conn_str, autocommit=False) as conn:
        from app.tools.db import _MIGRATIONS_DIR

        for mig in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            await conn.execute(mig.read_text())  # type: ignore[arg-type]
        await conn.commit()
        await conn.execute(
            "TRUNCATE reminder_scheduling_ledger, deadline_task_peers, reminder_outbox, "
            "recent_outbound, ops_alerts_throttle"
        )
        await conn.commit()
        yield conn


def _assert_cancel_call(cancel: AsyncMock, *, peer: str, page_id: str) -> None:
    from app.tools import reminders

    cancel.assert_awaited_once()
    call = cancel.await_args
    assert call is not None
    assert call.args == ()
    assert call.kwargs == {"peer": peer, "notion_page_id": page_id}
    real = inspect.signature(reminders.cancel_pending_nudges)
    assert set(call.kwargs) == set(real.parameters)
    real.bind(*call.args, **call.kwargs)


# ---------------------------------------------------------------------------
# Postgres round-trip
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_DB, reason="DATABASE_URL not set")
@pytest.mark.asyncio
async def test_cancel_pending_nudges_kills_the_series_and_nothing_else(
    db_conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.scheduler.reminder_scheduling import get_active_deadline_for_page, schedule_for_task
    from app.tools import reminders

    peer = f"<recipient-{uuid.uuid4().hex[:8]}>"
    page = str(uuid.uuid4())
    other_page = str(uuid.uuid4())
    now = datetime.now(UTC)
    deadline = now + timedelta(days=5)

    scheduled, failures = await schedule_for_task(
        db_conn,
        notion_page_id=page,
        peer=peer,
        deadline_at=deadline,
        urgency=80,
        now=now,
        user_tz="UTC",
        title="Placeholder task",
    )
    assert scheduled and not failures
    await schedule_for_task(
        db_conn,
        notion_page_id=other_page,
        peer=peer,
        deadline_at=deadline,
        urgency=80,
        now=now,
        user_tz="UTC",
    )
    # A reminder row on the same page belongs to cancel_pending_for_page.
    reminder_id = await reminders.enqueue(
        db_conn,
        notion_page_id=page,
        peer=peer,
        body="Test message",
        due_at=now + timedelta(hours=1),
        idempotency_key=f"test-{uuid.uuid4()}",
    )
    # Another peer's nudge on the same page id is not this conversation's.
    other_peer_id = await reminders.enqueue(
        db_conn,
        notion_page_id=page,
        peer="<other-recipient>",
        body="Test message",
        due_at=now + timedelta(hours=1),
        idempotency_key=f"test-{uuid.uuid4()}",
        kind="deadline",
    )
    await db_conn.commit()
    # One series row already delivered: history, left alone.
    delivered_id = scheduled[0].outbox_id
    await db_conn.execute(
        "UPDATE reminder_outbox SET state = 'delivered' WHERE id = %s", (str(delivered_id),)
    )
    await db_conn.commit()

    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])
    cancelled = await reminders.cancel_pending_nudges(peer=peer, notion_page_id=page)

    assert cancelled == len(scheduled) - 1
    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT id, notion_page_id, peer, kind, state, last_error FROM reminder_outbox"
        )
        rows = {str(r[0]): r[1:] for r in await cur.fetchall()}
    for item in scheduled[1:]:
        assert rows[str(item.outbox_id)][3:] == ("dead", "task completed")
    assert rows[str(delivered_id)][3] == "delivered"
    assert rows[str(reminder_id)][3] == "pending"
    assert rows[str(other_peer_id)][3] == "pending"
    other_page_states = {r[3] for r in rows.values() if r[0] == other_page}
    assert other_page_states == {"pending"}

    # The series reads as inactive; the other page's series is untouched.
    assert await get_active_deadline_for_page(db_conn, page) is None
    assert await get_active_deadline_for_page(db_conn, other_page) is not None

    # Idempotent: a second completion finds nothing left to cancel.
    assert await reminders.cancel_pending_nudges(peer=peer, notion_page_id=page) == 0


@pytest.mark.asyncio
async def test_cancel_pending_nudges_is_a_no_op_without_peer_or_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tools import reminders

    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    assert await reminders.cancel_pending_nudges(peer="", notion_page_id="<page>") == 0
    assert await reminders.cancel_pending_nudges(peer="<peer>", notion_page_id="") == 0


# ---------------------------------------------------------------------------
# Call sites (clause 10)
# ---------------------------------------------------------------------------


def _complete_state(**overrides: Any) -> State:
    state: dict[str, Any] = {
        "peer": "<recipient>",
        "incoming": "done",
        "intent": "COMPLETE",
        "messages": [],
        "active_task": None,
        "streak": 0,
        "tasks_completed_today": 0,
        "user_prefs": {},
        "mood": None,
        "available_minutes": None,
        "conversation_state": "idle",
        "pending_outbound": [],
        "recent_tasks": [{
            "page_id": "<page_T>",
            "title": "Placeholder task",
            "kind": "task",
            "event": "suggested",
            "at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        }],
    }
    state.update(overrides)
    return state  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_complete_node_cancels_nudges_for_a_task() -> None:
    from app.graph.nodes import complete as complete_module

    nudges = AsyncMock(return_value=2)
    reminder_cancel = AsyncMock(return_value=0)
    with (
        patch("app.tools.notion.update_status", AsyncMock(return_value={})),
        patch("app.tools.rewards.maybe_reward", AsyncMock(return_value=_REWARD)),
        patch.object(complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)),
        patch("app.tools.reminders.cancel_pending_nudges", nudges),
        patch("app.tools.reminders.cancel_pending_reminders", reminder_cancel),
        patch("app.tools.reminders.resolve_recent_outbound", AsyncMock(return_value=0)),
    ):
        result = await complete_module.complete_node(_complete_state())

    _assert_cancel_call(nudges, peer="<recipient>", page_id="<page_T>")
    # A task page has no reminder rows to cancel.
    reminder_cancel.assert_not_awaited()
    assert result["recent_tasks"][0]["event"] == "completed"


@pytest.mark.asyncio
async def test_complete_node_still_completes_when_the_nudge_cancel_fails() -> None:
    from app.graph.nodes import complete as complete_module

    update_status = AsyncMock(return_value={})
    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.rewards.maybe_reward", AsyncMock(return_value=_REWARD)),
        patch.object(complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)),
        patch("app.tools.reminders.cancel_pending_nudges", AsyncMock(side_effect=RuntimeError("db"))),
        patch("app.tools.reminders.resolve_recent_outbound", AsyncMock(return_value=0)),
        capture_logs() as logs,
    ):
        result = await complete_module.complete_node(_complete_state())

    update_status.assert_awaited_once_with(page_id="<page_T>", new_status="Completed")
    failed = [e for e in logs if e["event"] == "complete_node.nudge_cancel_failed"]
    assert failed == [{
        "event": "complete_node.nudge_cancel_failed",
        "log_level": "warning",
        "page_id": "<page_T>",
        "error_type": "RuntimeError",
    }]
    assert not any(e["event"] == "complete_node.error" for e in logs)
    assert result["pending_outbound"][0]["notion_page_title"] == "Placeholder task"


@pytest.mark.asyncio
async def test_log_finished_cancels_nudges_when_it_completes_an_open_task() -> None:
    from app.graph.nodes._log_finished import log_finished

    nudges = AsyncMock(return_value=1)
    match = SimpleNamespace(page_id="<page_T>", title="Placeholder task")
    with (
        patch("app.graph.nodes.intake._find_existing_task_match", AsyncMock(return_value=match)),
        patch("app.tools.notion.update_status", AsyncMock(return_value={})),
        patch("app.tools.rewards.maybe_reward", AsyncMock(return_value=_REWARD)),
        patch("app.tools.reminders.cancel_pending_nudges", nudges),
        patch("app.tools.reminders.resolve_recent_outbound", AsyncMock(return_value=0)),
    ):
        await log_finished(
            state=_complete_state(recent_tasks=[]),
            peer="<recipient>",
            title="Placeholder task",
            work_type="Independent",
            urgency=50,
            time_estimate=15,
            energy_required="Low",
            log_event="test.logged_finished",
        )

    _assert_cancel_call(nudges, peer="<recipient>", page_id="<page_T>")


@pytest.mark.asyncio
async def test_log_finished_new_page_has_no_nudges_to_cancel() -> None:
    from app.graph.nodes._log_finished import log_finished

    nudges = AsyncMock(return_value=0)
    with (
        patch("app.graph.nodes.intake._find_existing_task_match", AsyncMock(return_value=None)),
        patch("app.tools.notion.create_task", AsyncMock(return_value={"id": "<page_new>"})),
        patch("app.tools.rewards.maybe_reward", AsyncMock(return_value=_REWARD)),
        patch("app.tools.reminders.cancel_pending_nudges", nudges),
    ):
        await log_finished(
            state=_complete_state(recent_tasks=[]),
            peer="<recipient>",
            title="Placeholder task",
            work_type="Independent",
            urgency=50,
            time_estimate=15,
            energy_required="Low",
            log_event="test.logged_finished",
        )

    nudges.assert_not_awaited()


@pytest.mark.asyncio
async def test_log_finished_swallows_a_nudge_cancel_failure() -> None:
    from app.graph.nodes._log_finished import log_finished

    match = SimpleNamespace(page_id="<page_T>", title="Placeholder task")
    with (
        patch("app.graph.nodes.intake._find_existing_task_match", AsyncMock(return_value=match)),
        patch("app.tools.notion.update_status", AsyncMock(return_value={})),
        patch("app.tools.rewards.maybe_reward", AsyncMock(return_value=_REWARD)),
        patch("app.tools.reminders.cancel_pending_nudges", AsyncMock(side_effect=RuntimeError())),
        patch("app.tools.reminders.resolve_recent_outbound", AsyncMock(return_value=0)),
        capture_logs() as logs,
    ):
        result = await log_finished(
            state=_complete_state(recent_tasks=[]),
            peer="<recipient>",
            title="Placeholder task",
            work_type="Independent",
            urgency=50,
            time_estimate=15,
            energy_required="Low",
            log_event="test.logged_finished",
        )

    assert "log_finished.nudge_cancel_failed" in {e["event"] for e in logs}
    assert result["pending_outbound"][0]["notion_page_id"] == "<page_T>"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["task", "reminder"])
async def test_interaction_review_complete_cancels_nudges(kind: str) -> None:
    from app.graph import interaction_review as review

    nudges = AsyncMock(return_value=1)
    with (
        patch("app.tools.notion.update_status", AsyncMock(return_value={})),
        patch("app.tools.notion.get_page", AsyncMock(return_value={"properties": {}})),
        patch("app.tools.interaction_reviews.mark_executed", AsyncMock(return_value=None)),
        patch("app.tools.rewards.maybe_reward", AsyncMock(return_value=_REWARD)),
        patch("app.tools.reminders.cancel_pending_reminders", AsyncMock(return_value=0)),
        patch("app.tools.reminders.cancel_pending_nudges", nudges),
        patch("app.tools.reminders.resolve_recent_outbound", AsyncMock(return_value=0)),
    ):
        execution = await review._complete(
            review_id=uuid.uuid4(),
            peer="<recipient>",
            page_id="<page_T>",
            title="Placeholder task",
            kind=kind,
            state={},
            now=datetime.now(UTC),
            progress=review._Progress(),
        )

    _assert_cancel_call(nudges, peer="<recipient>", page_id="<page_T>")
    assert execution.page_id == "<page_T>"


@pytest.mark.asyncio
async def test_interaction_review_complete_swallows_a_nudge_cancel_failure() -> None:
    from app.graph import interaction_review as review

    with (
        patch("app.tools.notion.update_status", AsyncMock(return_value={})),
        patch("app.tools.notion.get_page", AsyncMock(return_value={"properties": {}})),
        patch("app.tools.interaction_reviews.mark_executed", AsyncMock(return_value=None)),
        patch("app.tools.rewards.maybe_reward", AsyncMock(return_value=_REWARD)),
        patch("app.tools.reminders.cancel_pending_nudges", AsyncMock(side_effect=RuntimeError())),
        patch("app.tools.reminders.resolve_recent_outbound", AsyncMock(return_value=0)),
        capture_logs() as logs,
    ):
        execution = await review._complete(
            review_id=uuid.uuid4(),
            peer="<recipient>",
            page_id="<page_T>",
            title="Placeholder task",
            kind="task",
            state={},
            now=datetime.now(UTC),
            progress=review._Progress(),
        )

    failed = [e for e in logs if e["event"] == "interaction_review.nudge_cancel_failed"]
    assert len(failed) == 1 and failed[0]["error_type"] == "RuntimeError"
    assert execution.reward_text == "Nice work!"
