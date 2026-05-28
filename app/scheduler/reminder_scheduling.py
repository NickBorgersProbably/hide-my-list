"""Shared reminder scheduling helper for deadline-driven reminder series.

schedule_for_task() is called by both:
  - app/graph/nodes/intake.py: inline scheduling immediately after task creation
  - app/scheduler/reminder_scheduler.py: nightly daemon backstop

The algorithm is: plan milestones via deadline_planner -> query the ledger for
nearby existing slots -> assign a load-balanced slot per milestone -> enqueue
outbox row (kind='deadline') -> insert ledger row.

The outbox kind='deadline' (migration 0008) is what tells the reminder worker
to skip notion.complete_reminder on delivery. Without it, the worker would
silently complete the user's task on the first milestone ping.

Callers decide whether to call notion.mark_reminder_scheduled() based on
whether enqueue_failures is empty.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import psycopg.rows
import structlog

log = structlog.get_logger(__name__)

# Window around each milestone ideal to query existing ledger slots for
# load-balancing: +-_LEDGER_WINDOW_HOURS hours (matches assign_slot
# max_drift_minutes default of 720 min = 12h).
_LEDGER_WINDOW_HOURS = 12


async def schedule_for_task(
    page_id: str,
    title: str,
    peer: str,
    deadline_at: datetime,
    urgency: int,
    *,
    now: datetime,
    user_tz: str,
) -> tuple[list[tuple[str, datetime]], list[str]]:
    """Plan and enqueue the milestone reminder series for one task.

    Calls deadline_planner.plan_milestones + assign_slot per milestone.
    Queries reminder_scheduling_ledger for nearby existing slots (load balance).
    Calls reminders.enqueue (kind='deadline') + inserts ledger row per milestone.

    Callers decide whether to call notion.mark_reminder_scheduled based on
    whether enqueue_failures is empty (empty = all milestones succeeded;
    non-empty = at least one failed, daemon will retry via backstop).

    Args:
        page_id:     Notion page ID of the task.
        title:       Task title for reminder body text.
        peer:        E.164 recipient phone number.
        deadline_at: Task deadline, must be tz-aware.
        urgency:     Integer urgency score (0-100).
        now:         Current time reference, must be tz-aware.
        user_tz:     IANA timezone string for the user.

    Returns:
        (assigned_slots, enqueue_failures)
        assigned_slots: list of (milestone_label, assigned_slot_datetime) for
            milestones that were successfully enqueued.
        enqueue_failures: list of milestone labels that failed to enqueue.
    """
    from app.scheduler.deadline_planner import (
        _env_defaults,
        assign_slot,
        plan_milestones,
        tier_for,
    )
    from app.tools.db import get_db_conn
    from app.tools.reminders import enqueue

    milestones = plan_milestones(deadline_at, urgency, now, user_tz=user_tz)
    env_defaults = _env_defaults()
    tier = tier_for(urgency)

    assigned_slots: list[tuple[str, datetime]] = []
    enqueue_failures: list[str] = []

    for m in milestones:
        try:
            # Query ledger for existing slots in the load-balancing window.
            existing = await _query_ledger_window(m.ideal_at)

            # Assign a load-balanced slot.
            slot, used_fallback = assign_slot(
                m.ideal_at,
                existing,
                deadline_at,
                user_tz=user_tz,
                now=now,
                **env_defaults,  # type: ignore[arg-type]
            )

            if used_fallback:
                log.warning(
                    "reminder_scheduling.slot_fallback_used",
                    page_id=page_id,
                    milestone=m.label,
                )

            # Build the human-readable reminder body (shame-safe, ADHD-friendly).
            humanized_deadline = _humanize_deadline(deadline_at, user_tz)
            body = f"{title} is coming up {humanized_deadline} - want to start now?"
            idempotency_key = (
                f"deadline-{page_id}-{m.label}-{deadline_at.isoformat()}"
            )

            async with get_db_conn() as conn:
                outbox_id = await enqueue(
                    conn,
                    notion_page_id=page_id,
                    peer=peer,
                    body=body,
                    due_at=slot,
                    idempotency_key=idempotency_key,
                    kind="deadline",
                )

                # Insert ledger row within the same connection / transaction.
                await _insert_ledger_row(
                    conn,
                    page_id=page_id,
                    deadline_at=deadline_at,
                    urgency=urgency,
                    tier=tier,
                    milestone_label=m.label,
                    ideal_slot_at=m.ideal_at,
                    assigned_slot_at=slot,
                    outbox_id=outbox_id,
                )

            assigned_slots.append((m.label, slot))
            log.info(
                "reminder_scheduling.milestone_enqueued",
                page_id=page_id,
                milestone=m.label,
                used_fallback=used_fallback,
            )

        except psycopg.errors.UniqueViolation:
            # idempotency_key already exists — milestone already scheduled.
            log.info(
                "reminder_scheduling.milestone_already_scheduled",
                page_id=page_id,
                milestone=m.label,
            )
            # Treat idempotent re-runs as success (not a failure).
            assigned_slots.append((m.label, m.ideal_at))

        except Exception:
            # Privacy: log only the milestone label (deterministic, not PII).
            # Do NOT log the exception string — psycopg / asyncio errors can
            # surface input substrings, and per DEV-AGENTS.md private content
            # must not be echoed to structlog.
            log.exception(
                "reminder_scheduling.milestone_enqueue_failed",
                page_id=page_id,
                milestone=m.label,
            )
            enqueue_failures.append(m.label)

    return assigned_slots, enqueue_failures


async def _query_ledger_window(
    ideal_at: datetime,
    *,
    window_hours: int = _LEDGER_WINDOW_HOURS,
) -> list[datetime]:
    """Return assigned_slot_at values from the ledger near the given ideal time.

    Queries non-superseded rows within +-window_hours of ideal_at to give
    assign_slot the load data it needs for bucket occupancy calculation.

    Returns an empty list if DATABASE_URL is not set (test/offline environments).
    """
    from app.tools.db import get_connection_string, get_db_conn

    try:
        get_connection_string()  # raises KeyError if not set
    except KeyError:
        return []

    window = timedelta(hours=window_hours)
    lo = ideal_at - window
    hi = ideal_at + window

    try:
        async with get_db_conn() as conn:
            async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                await cur.execute(
                    """
                    SELECT assigned_slot_at
                    FROM reminder_scheduling_ledger
                    WHERE superseded_at IS NULL
                      AND assigned_slot_at BETWEEN %s AND %s
                    """,
                    (lo, hi),
                )
                rows = await cur.fetchall()
        return [row["assigned_slot_at"] for row in rows]
    except Exception:
        # Privacy: do not log exception strings (may echo timestamps).
        log.exception("reminder_scheduling.ledger_query_failed")
        return []


async def _insert_ledger_row(
    conn: Any,
    *,
    page_id: str,
    deadline_at: datetime,
    urgency: int,
    tier: str,
    milestone_label: str,
    ideal_slot_at: datetime,
    assigned_slot_at: datetime,
    outbox_id: uuid.UUID,
) -> None:
    """Insert a row into reminder_scheduling_ledger."""
    await conn.execute(
        """
        INSERT INTO reminder_scheduling_ledger
          (notion_page_id, deadline_at, urgency, tier, milestone_label,
           ideal_slot_at, assigned_slot_at, reminder_outbox_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            page_id,
            deadline_at,
            urgency,
            tier,
            milestone_label,
            ideal_slot_at,
            assigned_slot_at,
            str(outbox_id),
        ),
    )


async def supersede_ledger_rows(page_id: str) -> list[uuid.UUID]:
    """Mark all active ledger rows for page_id as superseded (deadline changed).

    Returns the reminder_outbox_id values of superseded rows so callers can
    cancel the corresponding outbox rows (set state='dead').
    """
    now = datetime.now(UTC)
    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            await cur.execute(
                """
                UPDATE reminder_scheduling_ledger
                   SET superseded_at = %s
                 WHERE notion_page_id = %s
                   AND superseded_at IS NULL
                RETURNING reminder_outbox_id
                """,
                (now, page_id),
            )
            rows = await cur.fetchall()
    return [uuid.UUID(str(row["reminder_outbox_id"])) for row in rows]


async def cancel_outbox_rows(outbox_ids: list[uuid.UUID]) -> None:
    """Cancel outbox rows corresponding to superseded ledger rows.

    Sets state='dead' with a descriptive last_error so the reminder_worker
    skips delivery. Only affects rows still in pending/scheduled state.
    """
    if not outbox_ids:
        return

    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        for oid in outbox_ids:
            await conn.execute(
                """
                UPDATE reminder_outbox
                   SET state = 'dead',
                       last_error = 'superseded_by_deadline_change'
                 WHERE id = %s
                   AND state IN ('pending', 'scheduled')
                """,
                (str(oid),),
            )


async def get_active_deadline_for_page(page_id: str) -> datetime | None:
    """Return the deadline_at of the most recent non-superseded ledger row for page_id.

    Returns None if no active ledger rows exist (task not yet scheduled or
    all rows have been superseded).
    """
    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            await cur.execute(
                """
                SELECT deadline_at
                FROM reminder_scheduling_ledger
                WHERE notion_page_id = %s
                  AND superseded_at IS NULL
                ORDER BY scheduled_at DESC
                LIMIT 1
                """,
                (page_id,),
            )
            row = await cur.fetchone()

    if row is None:
        return None
    dt: datetime = row["deadline_at"]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _humanize_deadline(deadline_at: datetime, user_tz: str) -> str:
    """Return a natural-language description of how soon the deadline is.

    Examples:
        "this afternoon"
        "tomorrow"
        "in 3 days"
        "next week"
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(user_tz)
    now_local = datetime.now(UTC).astimezone(tz)
    deadline_local = deadline_at.astimezone(tz)

    delta = deadline_local - now_local
    days = delta.days

    if days < 0:
        return "soon"
    if days == 0:
        hour = deadline_local.hour
        if hour < 12:
            return "this morning"
        if hour < 17:
            return "this afternoon"
        return "this evening"
    if days == 1:
        return "tomorrow"
    if days <= 6:
        return f"in {days} days"
    if days <= 13:
        return "next week"
    if days <= 30:
        return f"in {days // 7} weeks"
    return f"in about {days // 30} month{'s' if days // 30 > 1 else ''}"
