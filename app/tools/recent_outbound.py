"""Reader for the `recent_outbound` table.

The reminder worker writes a row for every delivered reminder, recording which
Notion page is awaiting a reply and until when (`app/scheduler/reminder_worker.py`).
That row is how a terse reply in a later session — "done", "did it" — is tied back
to the reminder it answers, across the checkpoint boundary.

Until this module existed the table had exactly one reader, the retention job's
DELETE. Graph nodes had no way to see that a reminder was outstanding, so a
completion reply fell through to whatever `active_task` happened to be left in
the checkpoint and completed the wrong Notion page. See issue #641.

Private data discipline:
- `title` is the reminder body (the user's own words). It is returned to callers
  for reward attribution but must NEVER be logged.
- `peer` is a recipient identifier and must not be logged. Only non-personal operational fields (e.g., `notion_page_id`, `signal_timestamp`) may be emitted in log events.
"""
from __future__ import annotations

from datetime import datetime

import structlog
from typing_extensions import TypedDict

log = structlog.get_logger(__name__)


class AwaitingReply(TypedDict):
    """An outbound reminder that has not yet been resolved by a reply.

    title: the reminder body — PRIVATE, never log it.
    sent_at: when the reminder went out; used to decide whether this context or
        a checkpointed `active_task` is the more recent thing the user is
        plausibly responding to.
    """
    notion_page_id: str
    signal_timestamp: int
    title: str
    reminder_type: str
    sent_at: datetime


async def load_awaiting_reply(peer: str) -> AwaitingReply | None:
    """Return the most recent unresolved reminder for `peer`, or None.

    Only rows that are still awaiting a reply and have not expired are
    considered. `expires_at` is set 24h out by the reminder worker precisely to
    stop stale context from being matched against an unrelated later message.

    Returns None on any database failure: a completion turn must not break
    because this lookup is unavailable. The caller logs the degradation and
    falls back to its other candidate.
    """
    from app.tools.db import get_db_conn

    try:
        async with get_db_conn() as conn:
            cur = await conn.execute(
                """
                SELECT notion_page_id, signal_timestamp, title, reminder_type, sent_at
                  FROM recent_outbound
                 WHERE peer = %s
                   AND awaiting_reply IS TRUE
                   AND expires_at > now()
                 ORDER BY sent_at DESC
                 LIMIT 1
                """,
                (peer,),
            )
            row = await cur.fetchone()

        if row is None:
            return None

        return AwaitingReply(
            notion_page_id=row["notion_page_id"],
            signal_timestamp=row["signal_timestamp"],
            title=row["title"] or "",  # private — never logged
            reminder_type=row["reminder_type"],
            sent_at=row["sent_at"],
        )

    except Exception:
        # Log without the peer's message content.
        log.exception("recent_outbound.load_failed")
        return None


async def clear_awaiting_reply(*, peer: str, signal_timestamp: int) -> bool:
    """Mark one outbound row as resolved so it cannot match a later reply.

    Returns True when a row was updated. Failure is logged and reported as
    False rather than raised — the user's completion has already been
    acknowledged by the time this runs, and an exception here would turn a
    successful turn into an error path.
    """
    from app.tools.db import get_db_conn

    try:
        async with get_db_conn() as conn:
            cur = await conn.execute(
                """
                UPDATE recent_outbound
                   SET awaiting_reply = false
                 WHERE peer = %s
                   AND signal_timestamp = %s
                   AND awaiting_reply IS TRUE
                """,
                (peer, signal_timestamp),
            )
            updated: int = cur.rowcount

        if updated:
            log.info("recent_outbound.cleared")
            return True

        log.info("recent_outbound.clear_no_match")
        return False

    except Exception:
        log.exception("recent_outbound.clear_failed")
        return False
