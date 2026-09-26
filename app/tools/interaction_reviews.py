"""Interaction review verdict store (`interaction_reviews`, migration 0014).

The post-send interaction review (`app/graph/interaction_review.py`) records
every outcome here and reads it back for two limits: executed corrections per
peer in the last hour (the rate limit) and executed corrections across all
peers in the last 24 hours (the ops alert). The review reaches Postgres only
through these functions.

Privacy: `peer` and `reason` are private. `reason` is the model's own
explanation and can name a task. Neither is ever logged — log ids, counts, and
enum values only.
"""
from __future__ import annotations

import uuid
from typing import Literal

import structlog

log = structlog.get_logger(__name__)

ReviewVerdict = Literal["ok", "correct", "skipped", "error"]

# Longest `reason` stored. The model is asked for one sentence; anything longer
# is cut rather than rejected, because the row is the audit trail.
REASON_MAX_CHARS = 500


async def insert_review(
    *,
    peer: str,
    turn_ref: str,
    intent: str | None,
    verdict: ReviewVerdict,
    reason: str,
    action: str | None,
    action_page_id: str | None,
    executed: bool,
    follow_up_sent: bool,
) -> uuid.UUID:
    """Store one review outcome and return its id.

    Opens its own connection and commits. Raises on a database error; the
    caller decides whether a lost row matters.
    """
    from app.tools.db import get_db_conn

    review_id = uuid.uuid4()
    async with get_db_conn() as conn:
        await conn.execute(
            """
            INSERT INTO interaction_reviews
              (id, peer, turn_ref, intent, verdict, reason, action,
               action_page_id, executed, follow_up_sent)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                review_id,
                peer,
                turn_ref,
                intent,
                verdict,
                (reason or "")[:REASON_MAX_CHARS],
                action,
                action_page_id,
                executed,
                follow_up_sent,
            ),
        )
        await conn.commit()
    log.info(
        "interaction_reviews.inserted",
        review_id=str(review_id),
        verdict=verdict,
        action=action,
        executed=executed,
    )
    return review_id


async def count_executed_corrections(*, peer: str | None, window_seconds: float) -> int:
    """Count executed corrections in the last `window_seconds`.

    Scoped to one peer when `peer` is given, across every peer when it is None.
    Only rows with `verdict='correct'` and `executed=true` count: a proposed
    correction that yielded to a newer message changed nothing.
    """
    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        cursor = await conn.execute(
            """
            SELECT count(*) AS n FROM interaction_reviews
             WHERE verdict = 'correct'
               AND executed = true
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
