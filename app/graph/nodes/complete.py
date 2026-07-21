"""COMPLETE node: task completion + reward integration.

Marks the active task as completed in Notion, triggers the reward subsystem,
and drafts a celebration message into pending_outbound.

Reward integration implemented in PR-B5.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import structlog

from app.graph.state import ActiveTask, OutboundDraft, State

log = structlog.get_logger(__name__)

_ACTIVE_TASK_TTL = timedelta(hours=24)


@dataclass(frozen=True)
class _CompletionTarget:
    source: Literal["active_task", "recent_outbound"]
    page_id: str
    task_title: str
    work_type: str
    energy_required: str
    context_at: datetime | None
    signal_timestamp: int | None = None


def _parse_checkpoint_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _target_from_active_task(
    active_task: ActiveTask | None,
    *,
    now: datetime,
) -> _CompletionTarget | None:
    if not active_task:
        return None

    page_id = active_task.get("page_id", "")
    if not page_id:
        return None

    selected_at = _parse_checkpoint_datetime(active_task.get("selected_at"))
    if selected_at is not None and now - selected_at > _ACTIVE_TASK_TTL:
        log.info(
            "complete_node.active_task_stale",
            page_id=page_id,
            selected_at=selected_at.isoformat(),
        )
        return None

    return _CompletionTarget(
        source="active_task",
        page_id=page_id,
        # `.get(key, default)` only fires on a missing key, so a stored empty
        # title used to reach the reward path verbatim. task_title is private
        # data written to the manifest — pass the empty string through rather
        # than fabricating a placeholder that would be stored as if it were the
        # user's own words.
        task_title=(active_task.get("title") or "").strip(),
        work_type=active_task.get("work_type", ""),
        energy_required=active_task.get("energy_required", ""),
        context_at=selected_at,
    )


async def _load_recent_outbound_target(peer: str) -> _CompletionTarget | None:
    if not peer or not os.environ.get("DATABASE_URL"):
        return None

    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT signal_timestamp, notion_page_id, title, sent_at
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

    if not row:
        return None

    signal_timestamp = int(row["signal_timestamp"])
    sent_at = row["sent_at"]
    if not isinstance(sent_at, datetime):
        sent_at = _parse_checkpoint_datetime(sent_at)
    elif sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=UTC)
    else:
        sent_at = sent_at.astimezone(UTC)

    return _CompletionTarget(
        source="recent_outbound",
        page_id=str(row["notion_page_id"]),
        task_title=str(row.get("title") or "").strip(),
        work_type="",
        energy_required="",
        context_at=sent_at,
        signal_timestamp=signal_timestamp,
    )


async def _clear_recent_outbound(peer: str, signal_timestamp: int) -> None:
    if not peer or not os.environ.get("DATABASE_URL"):
        return

    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        await conn.execute(
            """
            UPDATE recent_outbound
               SET awaiting_reply = false
             WHERE peer = %s
               AND signal_timestamp = %s
            """,
            (peer, signal_timestamp),
        )
        await conn.commit()


def _choose_completion_target(
    *,
    active_target: _CompletionTarget | None,
    recent_target: _CompletionTarget | None,
) -> _CompletionTarget | None:
    if recent_target and active_target:
        if active_target.context_at is None:
            return recent_target
        if recent_target.context_at is None:
            return active_target
        if recent_target.context_at >= active_target.context_at:
            return recent_target
        return active_target
    return recent_target or active_target


def _clarify_completion_target(peer: str) -> dict[str, Any]:
    no_task_draft: OutboundDraft = {
        "recipient": peer,
        "body": "I can mark that done. Which task did you mean?",
        "notion_page_id": None,
    }
    return {
        "pending_outbound": [no_task_draft],
        "conversation_state": "idle",
        "active_task": None,
    }


async def complete_node(state: State) -> dict[str, Any]:
    """COMPLETE handler: update Notion, call rewards.maybe_reward(), draft reply."""
    peer = state.get("peer", "")

    try:
        from app.tools import notion
        from app.tools.rewards import maybe_reward

        active_task = state.get("active_task")
        now = datetime.now(UTC)
        active_target = _target_from_active_task(active_task, now=now)
        try:
            recent_target = await _load_recent_outbound_target(peer)
        except Exception:
            log.warning(
                "complete_node.recent_outbound_load_failed",
                active_page_id=active_target.page_id if active_target else None,
                exc_info=True,
            )
            return _clarify_completion_target(peer)

        target = _choose_completion_target(
            active_target=active_target,
            recent_target=recent_target,
        )

        log.info(
            "complete_node.resolved_target",
            source=target.source if target else None,
            page_id=target.page_id if target else None,
            active_page_id=active_target.page_id if active_target else None,
            recent_page_id=recent_target.page_id if recent_target else None,
        )

        if not target:
            return _clarify_completion_target(peer)

        page_id = target.page_id
        task_title = target.task_title

        if target.source == "active_task":
            await notion.update_status(page_id, "Completed")

        streak = state.get("streak", 0) + 1
        tasks_today = state.get("tasks_completed_today", 0) + 1

        reward_result = await maybe_reward(
            peer=peer,
            task_title=task_title,
            notion_page_id=page_id,
            streak=streak,
            work_type=target.work_type,
            energy_required=target.energy_required,
        )

        if target.source == "recent_outbound" and target.signal_timestamp is not None:
            try:
                await _clear_recent_outbound(peer, target.signal_timestamp)
            except Exception:
                log.warning(
                    "complete_node.recent_outbound_clear_failed",
                    page_id=page_id,
                    signal_timestamp=target.signal_timestamp,
                    exc_info=True,
                )

        reward_draft: OutboundDraft = {
            "recipient": peer,
            "body": reward_result["text"],
            "notion_page_id": page_id,
        }
        # Attach image if one was generated.
        # attachment_path is private; never log the path value.
        if reward_result["attachment_path"]:
            reward_draft["attachment_path"] = reward_result["attachment_path"]

        log.info(
            "complete_node.done",
            page_id=page_id,
            source=target.source,
            streak=streak,
        )
        return {
            "pending_outbound": [reward_draft],
            "active_task": None,
            "streak": streak,
            "tasks_completed_today": tasks_today,
            "conversation_state": "idle",
        }

    except Exception:
        log.exception("complete_node.error", peer=peer)
        fallback: OutboundDraft = {
            "recipient": peer,
            "body": "Got it, marked done! Nice work.",
            "notion_page_id": None,
        }
        return {
            "pending_outbound": [fallback],
            "active_task": None,
            "conversation_state": "idle",
        }
