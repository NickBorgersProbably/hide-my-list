"""Interaction review job store (`interaction_reviews`, migration 0014).

Each post-send interaction review (`app/graph/interaction_review.py`) is a
durable job. The listener stores a `pending` row before the review runs
(`create_pending`); every exit path moves the row to its final state
(`finalize`); on startup the listener reads what is still pending
(`list_pending`) and resumes or retires it. The review also reads the table back
for two limits: executed corrections per peer in the last hour (the rate limit)
and executed corrections across all peers in the last 24 hours (the ops alert).
The review reaches Postgres only through these functions.

Privacy: `peer` and `reason` are private. `reason` is the model's own
explanation and can name a task. Neither is ever logged — log ids, counts, and
enum values only.
"""
from __future__ import annotations

import uuid
from typing import Any, Literal, get_args

import structlog

log = structlog.get_logger(__name__)

# A row's state; `pending` until the review finishes, then one of the others.
ReviewState = Literal["pending", "ok", "correct", "skipped", "error"]
ReviewVerdict = Literal["ok", "correct", "skipped", "error"]
FINAL_VERDICTS: frozenset[str] = frozenset(get_args(ReviewVerdict))

# Longest `reason` stored. The model is asked for one sentence; anything longer
# is cut rather than rejected, because the row is the audit trail.
REASON_MAX_CHARS = 500


async def create_pending(*, peer: str, turn_ref: str, intent: str | None) -> uuid.UUID:
    """Store a pending review job for the turn at checkpoint `turn_ref`; return its id.

    Opens its own connection and commits. Raises on a database error.
    """
    from app.tools.db import get_db_conn

    review_id = uuid.uuid4()
    async with get_db_conn() as conn:
        await conn.execute(
            """
            INSERT INTO interaction_reviews (id, peer, turn_ref, intent, verdict)
            VALUES (%s, %s, %s, %s, 'pending')
            """,
            (review_id, peer, turn_ref, intent),
        )
        await conn.commit()
    log.info("interaction_reviews.pending_created", review_id=str(review_id))
    return review_id


async def finalize(
    review_id: uuid.UUID,
    *,
    verdict: ReviewVerdict,
    reason: str,
    action: str | None = None,
    action_page_id: str | None = None,
    executed: bool = False,
    follow_up_sent: bool = False,
) -> bool:
    """Move a pending row to its final state; return whether it was still pending.

    Idempotent: a row that is already final is left unchanged and the call
    returns False, so the review and the side that cancelled it can both
    finalize without overwriting each other. Raises on a database error.
    """
    from app.tools.db import get_db_conn

    if verdict not in FINAL_VERDICTS:
        raise ValueError(f"not a final verdict: {verdict!r}")
    async with get_db_conn() as conn:
        cursor = await conn.execute(
            """
            UPDATE interaction_reviews
               SET verdict = %s, reason = %s, action = %s, action_page_id = %s,
                   executed = %s, follow_up_sent = %s, updated_at = now()
             WHERE id = %s AND verdict = 'pending'
            """,
            (
                verdict,
                (reason or "")[:REASON_MAX_CHARS],
                action,
                action_page_id,
                executed,
                follow_up_sent,
                review_id,
            ),
        )
        updated = cursor.rowcount == 1
        await conn.commit()
    log.info(
        "interaction_reviews.finalized",
        review_id=str(review_id),
        verdict=verdict,
        action=action,
        executed=executed,
        updated=updated,
    )
    return updated


async def list_pending() -> list[dict[str, Any]]:
    """Every pending row, oldest first: `id`, `peer`, `turn_ref`, `intent`, `created_at`."""
    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        cursor = await conn.execute(
            """
            SELECT id, peer, turn_ref, intent, created_at
              FROM interaction_reviews
             WHERE verdict = 'pending'
             ORDER BY created_at ASC
            """
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def count_executed_corrections(*, peer: str | None, window_seconds: float) -> int:
    """Count executed corrections in the last `window_seconds`.

    Scoped to one peer when `peer` is given, across every peer when it is None.
    Every row with `executed=true` counts, whatever its final state: a
    correction whose Notion write ran and whose review then timed out or found
    a stale checkpoint still changed the user's data. A proposed correction
    that yielded before writing changed nothing and does not count.
    """
    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        cursor = await conn.execute(
            """
            SELECT count(*) AS n FROM interaction_reviews
             WHERE executed = true
               AND created_at > now() - make_interval(secs => %s)
               AND (%s::text IS NULL OR peer = %s::text)
            """,
            (window_seconds, peer, peer),
        )
        row = await cursor.fetchone()
    if not row:
        return 0
    value = row["n"] if isinstance(row, dict) else row[0]
    return int(value or 0)
