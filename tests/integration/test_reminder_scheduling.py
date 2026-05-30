"""Integration tests for app.scheduler.reminder_scheduling.

Real-Postgres round-trip coverage for the public helpers that move UUID +
timestamp values through the DB layer (test-rig clause 1):
  - schedule_for_task: inserts outbox + ledger rows, both with UUIDs and
    timestamps; round-trips the entire payload.
  - get_active_deadline_for_page: reads deadline_at from the ledger.
  - supersede_ledger_rows: marks rows superseded; returns UUIDs.
  - cancel_outbox_rows: updates outbox rows to state='dead'.

These tests intentionally do NOT mock the DB helpers — they exercise the
real Postgres connection so any UUID coercion / timestamp tz drift / column
type mismatch surfaces as a test failure rather than at runtime.

Notion is not exercised here (schedule_for_task does not call Notion). The
intake-side mark_reminder_scheduled call is covered separately in
tests/integration/test_intake.py via mocked Notion.

Skipped without DATABASE_URL.

Private data: all identifiers are placeholders (`<peer>`, `<page-XXXX>`).
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

_HAS_DB = bool(os.environ.get("DATABASE_URL", ""))
pytestmark = pytest.mark.skipif(
    not _HAS_DB,
    reason="DATABASE_URL not set; skipping integration tests",
)


# ---------------------------------------------------------------------------
# Fixture: clean DB with migrations applied
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db_conn() -> Any:
    """Provide a psycopg connection with migrations applied and tables clean.

    Mirrors the pattern from test_reminder_ledger_schema.py.
    """
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


# ---------------------------------------------------------------------------
# Test 1 — schedule_for_task: outbox + ledger round-trip (UUID + timestamp)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_for_task_round_trips_outbox_and_ledger(db_conn: Any) -> None:
    """End-to-end: schedule_for_task inserts outbox rows (kind='deadline')
    and ledger rows whose foreign-key references resolve, with all UUID and
    timestamp fields round-tripping through Postgres."""
    import psycopg.rows

    from app.scheduler.reminder_scheduling import schedule_for_task

    page_id = f"page-{uuid.uuid4()}"
    title = "Task placeholder"
    peer = "<peer>"
    now = datetime.now(UTC)
    deadline_at = now + timedelta(days=10)
    urgency = 50  # standard tier

    assigned_slots, failures = await schedule_for_task(
        page_id,
        title,
        peer,
        deadline_at,
        urgency,
        now=now,
        user_tz="America/Chicago",
    )

    assert failures == [], f"Unexpected scheduling failures: {failures}"
    assert assigned_slots, "Expected at least one milestone for a 10-day deadline"

    # Outbox rows must be kind='deadline'.
    async with db_conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        await cur.execute(
            "SELECT id, notion_page_id, kind, due_at, state, idempotency_key, body "
            "FROM reminder_outbox WHERE notion_page_id = %s",
            (page_id,),
        )
        outbox_rows = await cur.fetchall()

    assert len(outbox_rows) == len(assigned_slots), (
        f"Outbox row count {len(outbox_rows)} != "
        f"assigned milestone count {len(assigned_slots)}"
    )
    for row in outbox_rows:
        assert row["kind"] == "deadline", (
            f"Outbox row {row['id']!r} has kind={row['kind']!r}, "
            "expected 'deadline' (worker would otherwise complete the task on delivery)"
        )
        assert row["state"] == "pending"
        assert row["idempotency_key"].startswith(f"deadline-{page_id}-")
        # due_at must be tz-aware (Postgres TIMESTAMPTZ).
        assert row["due_at"].tzinfo is not None

    # Ledger rows must reference outbox rows by UUID (FK round-trip).
    async with db_conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        await cur.execute(
            "SELECT id, notion_page_id, deadline_at, urgency, tier, "
            "milestone_label, ideal_slot_at, assigned_slot_at, "
            "reminder_outbox_id, superseded_at "
            "FROM reminder_scheduling_ledger WHERE notion_page_id = %s",
            (page_id,),
        )
        ledger_rows = await cur.fetchall()

    assert len(ledger_rows) == len(assigned_slots)
    outbox_ids = {str(r["id"]) for r in outbox_rows}
    for lr in ledger_rows:
        assert str(lr["reminder_outbox_id"]) in outbox_ids, (
            "Ledger row references unknown outbox UUID"
        )
        assert lr["tier"] == "standard"
        assert lr["superseded_at"] is None
        # Timestamps round-trip with tzinfo.
        assert lr["deadline_at"].tzinfo is not None
        assert lr["assigned_slot_at"].tzinfo is not None
        assert lr["ideal_slot_at"].tzinfo is not None


# ---------------------------------------------------------------------------
# Test 2 — schedule_for_task is idempotent on duplicate runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_for_task_idempotent_on_rerun(db_conn: Any) -> None:
    """Calling schedule_for_task twice with the same (page_id, deadline)
    must not duplicate outbox/ledger rows — UniqueViolation on idempotency_key
    is caught and treated as success.

    Note: the second invocation reports the same milestones as "assigned" via
    the UniqueViolation branch but does NOT create new ledger rows. We assert
    on the table state rather than the return value.
    """
    import psycopg.rows

    from app.scheduler.reminder_scheduling import schedule_for_task

    page_id = f"page-{uuid.uuid4()}"
    peer = "<peer>"
    now = datetime.now(UTC)
    deadline_at = now + timedelta(days=10)

    first_slots, first_fail = await schedule_for_task(
        page_id, "title", peer, deadline_at, 50, now=now, user_tz="America/Chicago",
    )
    assert first_fail == []
    assert first_slots

    # Snapshot counts after first run.
    async with db_conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        await cur.execute(
            "SELECT COUNT(*) AS n FROM reminder_outbox WHERE notion_page_id = %s",
            (page_id,),
        )
        first_outbox = (await cur.fetchone())["n"]
        await cur.execute(
            "SELECT COUNT(*) AS n FROM reminder_scheduling_ledger "
            "WHERE notion_page_id = %s",
            (page_id,),
        )
        first_ledger = (await cur.fetchone())["n"]

    # Second run — same (page_id, deadline_at).
    _second_slots, second_fail = await schedule_for_task(
        page_id, "title", peer, deadline_at, 50, now=now, user_tz="America/Chicago",
    )
    assert second_fail == []

    async with db_conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        await cur.execute(
            "SELECT COUNT(*) AS n FROM reminder_outbox WHERE notion_page_id = %s",
            (page_id,),
        )
        second_outbox = (await cur.fetchone())["n"]
        await cur.execute(
            "SELECT COUNT(*) AS n FROM reminder_scheduling_ledger "
            "WHERE notion_page_id = %s",
            (page_id,),
        )
        second_ledger = (await cur.fetchone())["n"]

    assert second_outbox == first_outbox, "Idempotent re-run added outbox rows"
    assert second_ledger == first_ledger, "Idempotent re-run added ledger rows"


# ---------------------------------------------------------------------------
# Test 3 — get_active_deadline_for_page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_active_deadline_for_page_round_trips_timestamp(db_conn: Any) -> None:
    """get_active_deadline_for_page returns the deadline_at value with tzinfo,
    matching the original input (modulo Postgres TIMESTAMPTZ second granularity)."""
    from app.scheduler.reminder_scheduling import (
        get_active_deadline_for_page,
        schedule_for_task,
    )

    page_id = f"page-{uuid.uuid4()}"
    now = datetime.now(UTC)
    deadline_at = (now + timedelta(days=10)).replace(microsecond=0)

    await schedule_for_task(
        page_id, "title", "<peer>", deadline_at, 50,
        now=now, user_tz="America/Chicago",
    )

    result = await get_active_deadline_for_page(page_id)
    assert result is not None
    assert result.tzinfo is not None
    # Round-tripped value matches at second granularity.
    assert abs((result - deadline_at).total_seconds()) < 1.0


@pytest.mark.asyncio
async def test_get_active_deadline_returns_none_when_no_ledger_rows(db_conn: Any) -> None:
    """Returns None when no ledger row exists for the page."""
    from app.scheduler.reminder_scheduling import get_active_deadline_for_page

    result = await get_active_deadline_for_page(f"page-{uuid.uuid4()}")
    assert result is None


# ---------------------------------------------------------------------------
# Test 4 — supersede_ledger_rows returns UUIDs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supersede_ledger_rows_marks_and_returns_outbox_uuids(db_conn: Any) -> None:
    """supersede_ledger_rows must:
      - set superseded_at on every active ledger row for the page,
      - return the corresponding reminder_outbox_id UUIDs as uuid.UUID values.
    """
    import psycopg.rows

    from app.scheduler.reminder_scheduling import (
        schedule_for_task,
        supersede_ledger_rows,
    )

    page_id = f"page-{uuid.uuid4()}"
    now = datetime.now(UTC)
    deadline_at = now + timedelta(days=10)

    await schedule_for_task(
        page_id, "title", "<peer>", deadline_at, 50,
        now=now, user_tz="America/Chicago",
    )

    superseded_ids = await supersede_ledger_rows(page_id)
    assert superseded_ids, "Expected at least one superseded outbox UUID"
    for oid in superseded_ids:
        assert isinstance(oid, uuid.UUID), (
            f"supersede_ledger_rows returned {type(oid).__name__}, expected uuid.UUID"
        )

    # All active ledger rows now have superseded_at set.
    async with db_conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        await cur.execute(
            "SELECT COUNT(*) AS n FROM reminder_scheduling_ledger "
            "WHERE notion_page_id = %s AND superseded_at IS NULL",
            (page_id,),
        )
        active_remaining = (await cur.fetchone())["n"]

    assert active_remaining == 0, (
        f"Expected 0 active ledger rows after supersede, got {active_remaining}"
    )


# ---------------------------------------------------------------------------
# Test 5 — cancel_outbox_rows updates only matching pending/scheduled rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_outbox_rows_marks_state_dead(db_conn: Any) -> None:
    """cancel_outbox_rows must transition pending/scheduled rows to 'dead'
    with last_error='superseded_by_deadline_change'."""
    import psycopg.rows

    from app.scheduler.reminder_scheduling import (
        cancel_outbox_rows,
        schedule_for_task,
        supersede_ledger_rows,
    )

    page_id = f"page-{uuid.uuid4()}"
    now = datetime.now(UTC)
    deadline_at = now + timedelta(days=10)

    await schedule_for_task(
        page_id, "title", "<peer>", deadline_at, 50,
        now=now, user_tz="America/Chicago",
    )

    superseded_ids = await supersede_ledger_rows(page_id)
    await cancel_outbox_rows(superseded_ids)

    async with db_conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        await cur.execute(
            "SELECT state, last_error FROM reminder_outbox WHERE notion_page_id = %s",
            (page_id,),
        )
        rows = await cur.fetchall()

    assert rows, "Outbox rows missing after schedule_for_task"
    for r in rows:
        assert r["state"] == "dead", (
            f"Expected state='dead', got {r['state']!r}"
        )
        assert r["last_error"] == "superseded_by_deadline_change"


@pytest.mark.asyncio
async def test_cancel_outbox_rows_skips_already_delivered_rows(db_conn: Any) -> None:
    """cancel_outbox_rows must NOT clobber state when the row has already
    been delivered. Only pending/scheduled rows are affected."""
    import psycopg.rows

    from app.scheduler.reminder_scheduling import (
        cancel_outbox_rows,
        schedule_for_task,
        supersede_ledger_rows,
    )

    page_id = f"page-{uuid.uuid4()}"
    now = datetime.now(UTC)
    deadline_at = now + timedelta(days=10)

    await schedule_for_task(
        page_id, "title", "<peer>", deadline_at, 50,
        now=now, user_tz="America/Chicago",
    )

    # Force one row into 'delivered' state.
    async with db_conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        await cur.execute(
            "SELECT id FROM reminder_outbox WHERE notion_page_id = %s LIMIT 1",
            (page_id,),
        )
        target = (await cur.fetchone())["id"]
        await cur.execute(
            "UPDATE reminder_outbox SET state='delivered' WHERE id=%s",
            (target,),
        )
    await db_conn.commit()

    superseded_ids = await supersede_ledger_rows(page_id)
    await cancel_outbox_rows(superseded_ids)

    async with db_conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        await cur.execute(
            "SELECT state FROM reminder_outbox WHERE id = %s",
            (target,),
        )
        row = await cur.fetchone()

    assert row["state"] == "delivered", (
        "cancel_outbox_rows incorrectly clobbered a delivered row"
    )
