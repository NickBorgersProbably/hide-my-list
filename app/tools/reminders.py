"""Reminder outbox CRUD operations.

Provides enqueue, query, and state-transition helpers for the reminder_outbox
table. Used by both the scheduler job and the graph intake node.

All writes carry an explicit idempotency_key passed by the caller.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
import psycopg.rows


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
) -> uuid.UUID:
    """Insert a new reminder into the outbox with state='pending'.

    Returns the UUID of the inserted row. Raises psycopg.errors.UniqueViolation
    if notion_page_id already exists (each reminder maps to exactly one Notion page).

    Args:
        conn: Open async psycopg connection.
        notion_page_id: Notion page ID for this reminder (UNIQUE constraint).
        peer: E.164 recipient phone number.
        body: Reminder message text.
        due_at: When the reminder should be sent (UTC).
        idempotency_key: Unique key passed to signal-cli for deduplication.
        reminder_id: Optional UUID; generated if not provided.
    """
    rid = reminder_id or uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO reminder_outbox
          (id, notion_page_id, peer, body, due_at, state, idempotency_key)
        VALUES (%s, %s, %s, %s, %s, 'pending', %s)
        """,
        (str(rid), notion_page_id, peer, body, due_at, idempotency_key),
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
