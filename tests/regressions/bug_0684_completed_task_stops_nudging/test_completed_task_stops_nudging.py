"""Regression: completing a task stops its deadline nudges.

The reported failure: a task finished with deadline nudges still queued kept
getting nudged, because completion cancelled only `kind='reminder'` rows and
the worker checked the page only for reminders. Both halves run against real
Postgres here: the nudge is only visible when a worker cycle would send it.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.graph.state import State

_HAS_DB = bool(os.environ.get("DATABASE_URL", ""))
pytestmark = pytest.mark.skipif(not _HAS_DB, reason="DATABASE_URL not set")


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


async def _schedule_series(conn: Any, *, page: str, peer: str) -> list[uuid.UUID]:
    from app.scheduler.reminder_scheduling import schedule_for_task

    now = datetime.now(UTC)
    scheduled, failures = await schedule_for_task(
        conn,
        notion_page_id=page,
        peer=peer,
        deadline_at=now + timedelta(days=4),
        urgency=80,
        now=now,
        user_tz="UTC",
        title="Placeholder task",
    )
    assert scheduled and not failures
    return [item.outbox_id for item in scheduled]


async def _make_all_due(conn: Any, page: str) -> None:
    await conn.execute(
        "UPDATE reminder_outbox SET due_at = now() - interval '1 minute' "
        "WHERE notion_page_id = %s",
        (page,),
    )
    await conn.commit()


def _page(status: str) -> dict[str, Any]:
    return {"id": "<page>", "properties": {"Status": {"select": {"name": status}}}}


def _state(peer: str, page: str) -> State:
    state: dict[str, Any] = {
        "peer": peer,
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
            "page_id": page,
            "title": "Placeholder task",
            "kind": "task",
            "event": "added",
            "at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        }],
    }
    return state  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_done_kills_the_deadline_series(db_conn: Any) -> None:
    from app.graph.nodes.complete import complete_node
    from app.scheduler.reminder_worker import dispatch_due_reminders

    peer = f"<recipient-{uuid.uuid4().hex[:8]}>"
    page = str(uuid.uuid4())
    outbox_ids = await _schedule_series(db_conn, page=page, peer=peer)

    with (
        patch("app.tools.notion.update_status", AsyncMock(return_value={})),
        patch("app.tools.rewards.maybe_reward", AsyncMock(
            return_value={"text": "Nice work!", "attachment_path": None})),
    ):
        result = await complete_node(_state(peer, page))
    assert result["recent_tasks"][0]["event"] == "completed"

    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT state, last_error FROM reminder_outbox WHERE id = ANY(%s)",
            ([str(i) for i in outbox_ids],),
        )
        rows = await cur.fetchall()
    assert rows and all(tuple(r) == ("dead", "task completed") for r in rows), (
        "the task was completed but its deadline nudges will still fire"
    )

    await _make_all_due(db_conn, page)
    signal = AsyncMock(return_value={"timestamp": 1})
    with patch("app.tools.notion.get_page", AsyncMock(return_value=_page("Completed"))):
        await dispatch_due_reminders(db_conn, signal_send_fn=signal)
    signal.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_skips_a_nudge_whose_task_is_completed(db_conn: Any) -> None:
    """The backstop: the rows survived (cancel failed), the page reads Completed."""
    from app.scheduler.reminder_worker import dispatch_due_reminders

    peer = f"<recipient-{uuid.uuid4().hex[:8]}>"
    page = str(uuid.uuid4())
    outbox_ids = await _schedule_series(db_conn, page=page, peer=peer)
    await _make_all_due(db_conn, page)

    signal = AsyncMock(return_value={"timestamp": 1})
    with patch("app.tools.notion.get_page", AsyncMock(return_value=_page("Completed"))):
        await dispatch_due_reminders(db_conn, signal_send_fn=signal)

    signal.assert_not_awaited()
    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT state, last_error FROM reminder_outbox WHERE id = ANY(%s)",
            ([str(i) for i in outbox_ids],),
        )
        rows = await cur.fetchall()
    assert rows and all(tuple(r) == ("dead", "page already completed") for r in rows)
    async with db_conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM recent_outbound WHERE peer = %s", (peer,))
        count = await cur.fetchone()
    assert count is not None and count[0] == 0
