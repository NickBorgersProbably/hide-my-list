"""Interaction review job store (`interaction_reviews`, migration 0014).

Each post-send interaction review (`app/graph/interaction_review.py`) is a
durable job. The listener stores a `pending` row before the review runs
(`create_pending`). Before its first external effect the review claims the
row (`claim`: `executing`, with the action and page) and records each effect
as it lands (`mark_executed`, `mark_follow_up_sent`); every exit path moves
the row to its final state (`finalize`). On startup the listener reads the
unfinished rows (`list_unfinished`): a `pending` row was never claimed and
may be reviewed again; an `executing` row is finalized `error(interrupted)`
and never replayed. The review also reads the table back
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

# A row's state: `pending` until the review claims it, `executing` from the
# claim until it finishes, then one of the final verdicts.
ReviewState = Literal["pending", "executing", "ok", "correct", "skipped", "error"]
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


async def claim(review_id: uuid.UUID, *, action: str, page_id: str) -> bool:
    """Move a pending row to `executing` with its action and page; return whether it moved.

    Called before the correction's first external effect. False means the row
    is no longer pending (another side finalized it) and nothing may run.
    Raises on a database error.
    """
    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        cursor = await conn.execute(
            """
            UPDATE interaction_reviews
               SET verdict = 'executing', action = %s, action_page_id = %s,
                   updated_at = now()
             WHERE id = %s AND verdict = 'pending'
            """,
            (action, page_id or None, review_id),
        )
        claimed = cursor.rowcount == 1
        await conn.commit()
    log.info(
        "interaction_reviews.claimed", review_id=str(review_id), action=action, claimed=claimed
    )
    return claimed


async def mark_executed(review_id: uuid.UUID) -> None:
    """Record that a claimed row's Notion write ran. Raises on a database error."""
    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        await conn.execute(
            """
            UPDATE interaction_reviews SET executed = true, updated_at = now()
             WHERE id = %s AND verdict = 'executing'
            """,
            (review_id,),
        )
        await conn.commit()


async def mark_follow_up_sent(review_id: uuid.UUID, *, executed: bool = False) -> None:
    """Record that a claimed row's follow-up was delivered.

    `executed=True` also marks the correction executed, for `send_only`,
    whose whole correction is the delivery. Raises on a database error.
    """
    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        await conn.execute(
            """
            UPDATE interaction_reviews
               SET follow_up_sent = true, executed = executed OR %s, updated_at = now()
             WHERE id = %s AND verdict = 'executing'
            """,
            (executed, review_id),
        )
        await conn.commit()


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
    """Move a pending or executing row to its final state; return whether it moved.

    Idempotent: a row that is already final is left unchanged and the call
    returns False, so the review and the side that cancelled it can both
    finalize without overwriting each other. What a claim or a mark already
    stored is kept: `action` and `action_page_id` fall back to the stored
    values, and `executed` / `follow_up_sent` only ever turn true. Raises on a
    database error.
    """
    from app.tools.db import get_db_conn

    if verdict not in FINAL_VERDICTS:
        raise ValueError(f"not a final verdict: {verdict!r}")
    async with get_db_conn() as conn:
        cursor = await conn.execute(
            """
            UPDATE interaction_reviews
               SET verdict = %s, reason = %s,
                   action = COALESCE(%s, action),
                   action_page_id = COALESCE(%s, action_page_id),
                   executed = executed OR %s,
                   follow_up_sent = follow_up_sent OR %s,
                   updated_at = now()
             WHERE id = %s AND verdict IN ('pending', 'executing')
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


async def list_unfinished() -> list[dict[str, Any]]:
    """Every `pending` or `executing` row, oldest first.

    Each row: `id`, `peer`, `turn_ref`, `intent`, `verdict` (the state),
    `created_at`.
    """
    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        cursor = await conn.execute(
            """
            SELECT id, peer, turn_ref, intent, verdict, created_at
              FROM interaction_reviews
             WHERE verdict IN ('pending', 'executing')
             ORDER BY created_at ASC
            """
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def count_executed_corrections(*, peer: str | None, window_seconds: float) -> int:
    """Count executed corrections in the last `window_seconds`.

    Scoped to one peer when `peer` is given, across every peer when it is None.
    Every row with `executed=true` counts, whatever its state: a correction
    whose Notion write ran and whose review then timed out, found a stale
    checkpoint, or is still executing changed the user's data, and a
    delivered `send_only` follow-up is executed too. A proposed correction
    that yielded before acting changed nothing and does not count.
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
