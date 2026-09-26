"""Reminder outbox CRUD operations.

Provides enqueue, query, and state-transition helpers for the reminder_outbox
table, and the `recent_outbound` reads and writes COMPLETE needs. Used by the
scheduler jobs and the graph intake and complete nodes; graph nodes reach
Postgres only through functions like these.

All writes carry an explicit idempotency_key passed by the caller.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
import psycopg.rows
import structlog

log = structlog.get_logger(__name__)


class ReminderRow(psycopg.rows.DictRow):
    """Type alias for a reminder_outbox row returned as a dict."""


def _now() -> datetime:
    return datetime.now(UTC)


async def enqueue(
    conn: psycopg.AsyncConnection[Any],
    *,
    notion_page_id: str,
    peer: str,
    body: str,
    due_at: datetime,
    idempotency_key: str,
    reminder_id: uuid.UUID | None = None,
    kind: str = "reminder",
) -> uuid.UUID:
    """Insert a new reminder into the outbox with state='pending'.

    Returns the UUID of the inserted row. Raises psycopg.errors.UniqueViolation
    if idempotency_key already exists. Duplicate idempotency_key raises
    UniqueViolation (deadline daemon uses key format
    "deadline-<page_id>-<milestone>-<deadline_iso>" for at-most-once enqueue
    per task+milestone+deadline tuple).

    Args:
        conn: Open async psycopg connection.
        notion_page_id: Notion page ID for this reminder.
        peer: E.164 recipient phone number.
        body: Reminder message text.
        due_at: When the reminder should be sent (UTC).
        idempotency_key: Unique key per reminder; UNIQUE constraint prevents duplicate inserts.
        reminder_id: Optional UUID; generated if not provided.
        kind: "reminder" for wall-clock reminders, "deadline" for milestone pings.
    """
    if kind not in ("reminder", "deadline"):
        raise ValueError("kind must be 'reminder' or 'deadline'")
    rid = reminder_id or uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO reminder_outbox
          (id, notion_page_id, peer, body, due_at, state, idempotency_key, kind)
        VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s)
        """,
        (str(rid), notion_page_id, peer, body, due_at, idempotency_key, kind),
    )
    log.info(
        "reminders.enqueued",
        reminder_id=str(rid),
        due_at=due_at.isoformat(),
    )
    return rid


async def get_due(
    conn: psycopg.AsyncConnection[Any],
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Fetch pending/scheduled reminders that are now due.

    Does NOT acquire row locks — locking happens in the worker's claim step.
    """
    now = _now()
    async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        await cur.execute(
            """
            SELECT * FROM reminder_outbox
            WHERE state IN ('pending', 'scheduled')
              AND due_at <= %s
            ORDER BY due_at ASC
            LIMIT %s
            """,
            (now, limit),
        )
        return await cur.fetchall()


async def mark_delivered(
    conn: psycopg.AsyncConnection[Any],
    *,
    reminder_id: uuid.UUID,
    signal_timestamp: int,
) -> None:
    """Transition a reminder to delivered state."""
    now = _now()
    await conn.execute(
        """
        UPDATE reminder_outbox
           SET state = 'delivered',
               signal_timestamp = %s,
               delivered_at = %s,
               locked_until = NULL,
               worker_id = NULL
         WHERE id = %s
        """,
        (signal_timestamp, now, str(reminder_id)),
    )


async def mark_failed(
    conn: psycopg.AsyncConnection[Any],
    *,
    reminder_id: uuid.UUID,
    error: str,
    next_due_at: datetime,
    attempt: int,
) -> None:
    """Transition a reminder back to scheduled with updated attempt count and backoff."""
    await conn.execute(
        """
        UPDATE reminder_outbox
           SET state = 'scheduled',
               last_error = %s,
               due_at = %s,
               attempt = %s,
               locked_until = NULL,
               worker_id = NULL
         WHERE id = %s
        """,
        (error, next_due_at, attempt, str(reminder_id)),
    )


async def cancel_pending_for_page(
    conn: psycopg.AsyncConnection[Any],
    *,
    notion_page_id: str,
    peer: str,
) -> int:
    """Kill the undelivered reminder rows for a page the user already finished.

    A reminder completed before its time must not fire afterwards. Only
    `kind='reminder'` rows still waiting to go out (`pending` or `scheduled`)
    are touched: a `delivering` row is already in the worker's hands,
    delivered rows are history, and `kind='deadline'` rows belong to the
    deadline series. Scoped to `peer` so one conversation never cancels
    another's rows. The caller commits.

    Returns the number of rows cancelled.
    """
    cursor = await conn.execute(
        """
        UPDATE reminder_outbox
           SET state = 'dead',
               last_error = 'completed by user',
               locked_until = NULL,
               worker_id = NULL
         WHERE notion_page_id = %s
           AND peer = %s
           AND kind = 'reminder'
           AND state IN ('pending', 'scheduled')
        """,
        (notion_page_id, peer),
    )
    cancelled = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    log.info(
        "reminders.cancelled_for_page",
        notion_page_id=notion_page_id,
        cancelled_count=cancelled,
    )
    return cancelled


async def mark_dead(
    conn: psycopg.AsyncConnection[Any],
    *,
    reminder_id: uuid.UUID,
    error: str,
) -> None:
    """Transition a reminder to dead state (max attempts exceeded)."""
    await conn.execute(
        """
        UPDATE reminder_outbox
           SET state = 'dead',
               last_error = %s,
               locked_until = NULL,
               worker_id = NULL
         WHERE id = %s
        """,
        (error, str(reminder_id)),
    )


async def fetch_recent_outbound(peer: str, max_age_seconds: float) -> list[dict[str, Any]]:
    """Return recent_outbound rows for *peer* sent within the last max_age_seconds."""
    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT notion_page_id, reminder_type, sent_at
                  FROM recent_outbound
                 WHERE peer = %s
                   AND sent_at > now() - make_interval(secs => %s)
                 ORDER BY sent_at ASC, signal_timestamp ASC
                """,
                (peer, max_age_seconds),
            )
            rows = await cur.fetchall()
    return [dict(row) for row in rows]


def _db_configured(peer: str) -> bool:
    """Whether a peer-scoped lookup can run: a peer to scope by and a database."""
    return bool(peer) and bool(os.environ.get("DATABASE_URL"))


async def load_recent_outbound(peer: str) -> dict[str, Any] | None:
    """Return the newest delivery for *peer* still awaiting a reply, or None.

    Only rows with `awaiting_reply = true` and an unexpired `expires_at`
    count. The returned dict carries `signal_timestamp`, `notion_page_id`,
    `title` (the sent body, not the task title), `sent_at`, and
    `reminder_type` (the outbox kind the worker recorded). None when there is
    no such row, no peer, or no configured database.
    """
    if not _db_configured(peer):
        return None

    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT signal_timestamp, notion_page_id, title, sent_at, reminder_type
                  FROM recent_outbound
                 WHERE peer = %s
                   AND awaiting_reply = true
                   AND expires_at > now()
                 ORDER BY sent_at DESC, signal_timestamp DESC
                 LIMIT 1
                """,
                (peer,),
            )
            row = await cur.fetchone()
    return dict(row) if row else None


async def resolve_recent_outbound(
    peer: str, *, signal_timestamp: int, notion_page_id: str
) -> int:
    """Mark every live delivery a completion answers `awaiting_reply = false`.

    Scoped by page, not by the single delivery the user replied to: a task can
    have several reminders in flight (deadline milestones stack), and each
    delivery writes its own row. A sibling left live resolves a later,
    unrelated "done" to a task already finished. `signal_timestamp` stays in
    the predicate as a fallback for a caller with no page id.

    Returns the number of rows resolved; 0 with no peer or no database.
    """
    if not _db_configured(peer):
        return 0

    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        cursor = await conn.execute(
            """
            UPDATE recent_outbound
               SET awaiting_reply = false
             WHERE peer = %s
               AND awaiting_reply = true
               AND (
                     signal_timestamp = %s
                     OR (%s <> '' AND notion_page_id = %s)
                   )
            """,
            (peer, signal_timestamp, notion_page_id, notion_page_id),
        )
        await conn.commit()
    return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0


async def cancel_pending_reminders(peer: str, notion_page_id: str) -> int:
    """Kill the undelivered outbox rows of a reminder the user finished early.

    Opens a connection, runs `cancel_pending_for_page`, and commits. Returns
    the number of rows cancelled; 0 with no peer, no page id, or no database.
    Raises on a database error so the caller can retry and alert.
    """
    if not notion_page_id or not _db_configured(peer):
        return 0

    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        cancelled = await cancel_pending_for_page(
            conn, notion_page_id=notion_page_id, peer=peer
        )
        await conn.commit()
    return cancelled
