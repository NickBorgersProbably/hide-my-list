"""Integration tests for app.scheduler.reminder_scheduler (daemon backstop).

These tests focus on the dual-query edit-detection path that closed PR #601
omitted (doc-001 / d-002): without the second query
(`query_scheduled_tasks_with_deadlines`), tasks whose Due At is edited AFTER
the initial schedule are never re-detected, because the orphan query filters
out anything with `Reminder Scheduled At` set.

Notion is mocked at the verb level (notion.query_tasks_with_unscheduled_deadlines,
notion.query_scheduled_tasks_with_deadlines, notion.mark_reminder_scheduled).
Postgres is real — the ledger and outbox round-trips are the load-bearing
behavior, and the test-rig contract (clause 1) requires real-DB coverage for
UUID + timestamp fields.

Skipped without DATABASE_URL.

Private data: all identifiers are placeholders.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

_HAS_DB = bool(os.environ.get("DATABASE_URL", ""))
pytestmark = pytest.mark.skipif(
    not _HAS_DB,
    reason="DATABASE_URL not set; skipping integration tests",
)


@pytest.fixture()
async def db_conn() -> Any:
    """Clean DB with all migrations applied."""
    import psycopg

    from app.tools.db import _MIGRATIONS_DIR

    conn_str = os.environ["DATABASE_URL"]
    async with await psycopg.AsyncConnection.connect(conn_str, autocommit=False) as conn:
        for mig in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            await conn.execute(mig.read_text())  # type: ignore[arg-type]
        await conn.commit()
        await conn.execute(
            "TRUNCATE reminder_scheduling_ledger, reminder_outbox, "
            "recent_outbound, ops_alerts_throttle"
        )
        await conn.commit()
        yield conn


def _notion_page(page_id: str, due_at: datetime, urgency: int = 50) -> dict[str, Any]:
    """Synthesize a minimal Notion query result row."""
    return {
        "id": page_id,
        "properties": {
            "Title": {"title": [{"plain_text": "Task placeholder"}]},
            "Due At": {"date": {"start": due_at.isoformat()}},
            "Urgency": {"number": urgency},
        },
    }


@pytest.fixture(autouse=True)
def _peer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide DEFAULT_PEER env var so the daemon can resolve a recipient."""
    monkeypatch.setenv("DEFAULT_PEER", "<peer>")


# ---------------------------------------------------------------------------
# Test 1 — orphan catch-up: ledger + outbox created, mark_reminder_scheduled called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_schedules_orphans(
    db_conn: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Notion task with Due At set but no Reminder Scheduled At is the
    'orphan' case (intake's inline scheduling failed). The daemon must
    schedule the milestone series and call mark_reminder_scheduled."""
    import psycopg.rows

    from app.scheduler.reminder_scheduler import run_reminder_scheduler
    from app.tools import notion

    page_id = f"page-{uuid.uuid4()}"
    due_at = datetime.now(UTC) + timedelta(days=10)

    monkeypatch.setattr(
        notion, "query_tasks_with_unscheduled_deadlines",
        AsyncMock(return_value={"results": [_notion_page(page_id, due_at)]}),
    )
    monkeypatch.setattr(
        notion, "query_scheduled_tasks_with_deadlines",
        AsyncMock(return_value={"results": []}),
    )
    mark_called = AsyncMock(return_value={})
    monkeypatch.setattr(notion, "mark_reminder_scheduled", mark_called)

    await run_reminder_scheduler()

    async with db_conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        await cur.execute(
            "SELECT COUNT(*) AS n FROM reminder_scheduling_ledger "
            "WHERE notion_page_id = %s AND superseded_at IS NULL",
            (page_id,),
        )
        ledger_count = (await cur.fetchone())["n"]
        await cur.execute(
            "SELECT COUNT(*) AS n FROM reminder_outbox "
            "WHERE notion_page_id = %s AND kind = 'deadline'",
            (page_id,),
        )
        outbox_count = (await cur.fetchone())["n"]

    assert ledger_count > 0, "Daemon did not insert ledger rows for orphan"
    assert outbox_count == ledger_count
    mark_called.assert_awaited_once_with(page_id)


# ---------------------------------------------------------------------------
# Test 2 — dual-query edit detection: changed Due At supersedes + reschedules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_detects_deadline_edit_and_reschedules(
    db_conn: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key fix for doc-001/d-002: a task whose Due At was edited in Notion
    AFTER its initial schedule must be detected via the second
    (query_scheduled_tasks_with_deadlines) query, then superseded and
    rescheduled against the new deadline.

    The previous design (single orphan query only) would silently miss this
    because the orphan filter excludes tasks with Reminder Scheduled At set.
    """
    import psycopg.rows

    from app.scheduler.reminder_scheduler import run_reminder_scheduler
    from app.scheduler.reminder_scheduling import schedule_for_task
    from app.tools import notion

    page_id = f"page-{uuid.uuid4()}"
    original_deadline = (datetime.now(UTC) + timedelta(days=20)).replace(microsecond=0)
    new_deadline = (datetime.now(UTC) + timedelta(days=5)).replace(microsecond=0)

    # Seed: schedule against the original deadline (simulates a prior successful run).
    await schedule_for_task(
        page_id,
        "Task placeholder",
        "<peer>",
        original_deadline,
        50,
        now=datetime.now(UTC),
        user_tz="America/Chicago",
    )

    # Mock Notion: orphan list empty, scheduled list shows the page with the
    # NEW deadline.
    monkeypatch.setattr(
        notion, "query_tasks_with_unscheduled_deadlines",
        AsyncMock(return_value={"results": []}),
    )
    monkeypatch.setattr(
        notion, "query_scheduled_tasks_with_deadlines",
        AsyncMock(return_value={"results": [_notion_page(page_id, new_deadline)]}),
    )
    monkeypatch.setattr(notion, "mark_reminder_scheduled", AsyncMock(return_value={}))

    await run_reminder_scheduler()

    # Old ledger rows must be superseded.
    async with db_conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        await cur.execute(
            "SELECT COUNT(*) AS n FROM reminder_scheduling_ledger "
            "WHERE notion_page_id = %s AND superseded_at IS NOT NULL",
            (page_id,),
        )
        superseded = (await cur.fetchone())["n"]
        await cur.execute(
            "SELECT COUNT(*) AS n FROM reminder_scheduling_ledger "
            "WHERE notion_page_id = %s AND superseded_at IS NULL",
            (page_id,),
        )
        active = (await cur.fetchone())["n"]
        # Old outbox rows must be marked dead.
        await cur.execute(
            "SELECT COUNT(*) AS n FROM reminder_outbox "
            "WHERE notion_page_id = %s AND state = 'dead' "
            "AND last_error = 'superseded_by_deadline_change'",
            (page_id,),
        )
        dead = (await cur.fetchone())["n"]
        # New outbox rows must be pending and reference the new deadline via key.
        await cur.execute(
            "SELECT idempotency_key FROM reminder_outbox "
            "WHERE notion_page_id = %s AND state = 'pending'",
            (page_id,),
        )
        new_rows = await cur.fetchall()

    assert superseded > 0, "Old ledger rows were not superseded after deadline edit"
    assert active > 0, "No fresh ledger rows after deadline edit"
    assert dead > 0, "Old outbox rows were not marked dead"
    # New idempotency keys must encode the NEW deadline.
    for r in new_rows:
        assert new_deadline.isoformat() in r["idempotency_key"], (
            "New outbox rows do not encode the new deadline in idempotency_key"
        )


# ---------------------------------------------------------------------------
# Test 3 — tolerance: tiny drift in Notion Due At (within ±60s) is NOT an edit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_treats_subsecond_drift_as_unchanged(
    db_conn: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Notion + Postgres round-tripping introduces sub-second drift. The
    daemon's tolerance window (60s) must absorb that — no supersede, no
    reschedule, no churn."""
    import psycopg.rows

    from app.scheduler.reminder_scheduler import run_reminder_scheduler
    from app.scheduler.reminder_scheduling import schedule_for_task
    from app.tools import notion

    page_id = f"page-{uuid.uuid4()}"
    original_deadline = (datetime.now(UTC) + timedelta(days=10)).replace(microsecond=0)
    notion_deadline = original_deadline + timedelta(seconds=5)  # within tolerance

    await schedule_for_task(
        page_id, "Task placeholder", "<peer>", original_deadline, 50,
        now=datetime.now(UTC), user_tz="America/Chicago",
    )

    monkeypatch.setattr(
        notion, "query_tasks_with_unscheduled_deadlines",
        AsyncMock(return_value={"results": []}),
    )
    monkeypatch.setattr(
        notion, "query_scheduled_tasks_with_deadlines",
        AsyncMock(return_value={"results": [_notion_page(page_id, notion_deadline)]}),
    )
    mark_called = AsyncMock(return_value={})
    monkeypatch.setattr(notion, "mark_reminder_scheduled", mark_called)

    await run_reminder_scheduler()

    # Nothing got superseded.
    async with db_conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        await cur.execute(
            "SELECT COUNT(*) AS n FROM reminder_scheduling_ledger "
            "WHERE notion_page_id = %s AND superseded_at IS NOT NULL",
            (page_id,),
        )
        superseded = (await cur.fetchone())["n"]

    assert superseded == 0, "Sub-tolerance drift was incorrectly treated as edit"
    mark_called.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test 4 — Notion query failure does not crash daemon
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_handles_notion_query_failure(
    db_conn: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Notion 5xx on the orphan query must result in an ops alert and a
    clean exit — not an unhandled exception that crashes the scheduler job."""
    from app.scheduler.reminder_scheduler import run_reminder_scheduler
    from app.tools import notion, ops_alerts

    monkeypatch.setattr(
        notion, "query_tasks_with_unscheduled_deadlines",
        AsyncMock(side_effect=RuntimeError("notion 502")),
    )
    monkeypatch.setattr(
        notion, "query_scheduled_tasks_with_deadlines",
        AsyncMock(return_value={"results": []}),
    )

    ops_enqueue = AsyncMock(return_value=None)
    monkeypatch.setattr(ops_alerts, "enqueue", ops_enqueue)

    # Must not raise.
    await run_reminder_scheduler()

    # ops alert was enqueued.
    assert ops_enqueue.await_count >= 1
