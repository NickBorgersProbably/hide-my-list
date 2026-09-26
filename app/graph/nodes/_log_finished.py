"""Shared path for logging an accomplishment that was never on the list.

Two nodes reach it. ADD_TASK does when the user answers a completion question
with "it's new, just log it" and the intake model marks the save
`already_finished`. COMPLETE does when the user says yes to "Want me to log
'<title>' as done?" after reporting something that matched no open task. In
both the work is already done, so recording it as an open task would turn an
accomplishment into another obligation: it is stored Completed and celebrated
exactly like any other completion.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from app.graph.context import record_task_event, record_turn_action
from app.graph.state import OutboundDraft, State

log = structlog.get_logger(__name__)


async def log_finished(
    *,
    state: State,
    peer: str,
    title: str,
    work_type: str,
    urgency: int,
    time_estimate: int,
    energy_required: str,
    log_event: str,
) -> dict[str, Any]:
    """Record a finished item as Completed and celebrate it.

    When the title matches an open task at the intake dedup threshold, that
    task is completed instead of a second page being created, so a report
    still lands on the page the user already has. No sub-tasks, deadline
    series, or reminder are created: the work is done.

    The reward call carries the same kwargs as complete_node's, and the reply
    is complete_node's celebration body with `notion_page_title` set, so
    send_node names the task. `log_event` is the caller's event name; it logs
    booleans and counts only.

    Runs inside the caller's try/except; a raise here takes that node's
    fallback rather than claiming a completion.
    """
    from app.graph.nodes.complete import _celebration_body
    from app.graph.nodes.intake import _find_existing_task_match
    from app.tools import notion
    from app.tools.rewards import maybe_reward

    dedup_match = await _find_existing_task_match(title)
    if dedup_match is not None:
        page_id = dedup_match.page_id
        display_title = dedup_match.title
        await notion.update_status(page_id=page_id, new_status="Completed")
        turn_actions = record_turn_action(
            state.get("turn_actions"),
            action="notion.update_status",
            page_id=page_id,
            status="Completed",
        )
        try:
            from app.tools import reminders
            await reminders.resolve_recent_outbound(
                peer=peer,
                signal_timestamp=0,
                notion_page_id=page_id,
            )
        except Exception:
            log.warning(
                "log_finished.resolve_outbound_failed",
                has_peer=bool(peer),
                exc_info=True,
            )
    else:
        notion_page = await notion.create_task(
            title=title,
            work_type=work_type,
            urgency=urgency,
            time_estimate=time_estimate,
            energy_required=energy_required,
            status="Completed",
        )
        page_id = str((notion_page or {}).get("id") or "")
        display_title = title
        # Created already Completed: the status marks it as finished this turn.
        turn_actions = record_turn_action(
            state.get("turn_actions"),
            action="notion.create_task",
            page_id=page_id,
            status="Completed",
        )

    streak = state.get("streak", 0) + 1
    tasks_today = state.get("tasks_completed_today", 0) + 1
    reward_result = await maybe_reward(
        peer=peer,
        task_title=display_title,
        notion_page_id=page_id,
        streak=streak,
        work_type=work_type,
        energy_required=energy_required,
    )
    turn_actions = record_turn_action(turn_actions, action="reward", page_id=page_id)

    draft: OutboundDraft = {
        "recipient": peer,
        "body": _celebration_body(display_title, reward_result["text"]),
        "notion_page_id": page_id or None,
        # send_node substitutes the token and guarantees the name appears.
        "notion_page_title": display_title,
    }
    # attachment_path is private; never log the path value.
    if reward_result["attachment_path"]:
        draft["attachment_path"] = reward_result["attachment_path"]

    recent_tasks = list(state.get("recent_tasks") or [])
    if page_id:
        recent_tasks = record_task_event(
            recent_tasks,
            page_id=page_id,
            title=display_title,
            kind="task",
            event="completed",
            now=datetime.now(UTC),
        )

    log.info(
        log_event,
        duplicate_matched=dedup_match is not None,
        has_page_id=bool(page_id),
        has_attachment=bool(reward_result["attachment_path"]),
        streak=streak,
    )
    update: dict[str, Any] = {
        "pending_outbound": [draft],
        "streak": streak,
        "tasks_completed_today": tasks_today,
        "conversation_state": "idle",
        "pending_clarification": None,
        "recent_tasks": recent_tasks,
        "turn_actions": turn_actions,
    }
    active_task = state.get("active_task") or {}
    if page_id and active_task.get("page_id") == page_id:
        update["active_task"] = None
    return update
