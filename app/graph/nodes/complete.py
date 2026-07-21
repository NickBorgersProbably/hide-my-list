"""COMPLETE node: task completion + reward integration.

Resolves *which* task the user just finished, updates Notion when that task is
not already closed, triggers the reward subsystem, and drafts a celebration
message into pending_outbound.

Target resolution: a completion reply has two possible referents, and picking
the wrong one closes a task the user never touched.

1. An unresolved reminder in `recent_outbound` — the user is answering a nudge
   the reminder worker sent them, possibly in an earlier session. That Notion
   page was already set to Completed at delivery time, so this branch must not
   write to Notion again; it acknowledges, rewards, and marks the row resolved.
2. The checkpointed `active_task` — the user is finishing the task they were
   most recently offered by `selection_node`.

Whichever context is *more recent* wins. An `active_task` older than
`_ACTIVE_TASK_TTL` is stale and is not a completion target at all: it was
selected long enough ago that a terse "done" is more likely about something
else. When neither candidate is usable the node asks which task the user means
rather than closing whatever is left in the checkpoint — an unanswered question
is recoverable, a wrongly-completed task is not.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import structlog
from typing_extensions import TypedDict

from app.graph.state import ActiveTask, OutboundDraft, State
from app.tools.recent_outbound import AwaitingReply

log = structlog.get_logger(__name__)

# An active_task older than this is not a completion target. Mirrors the 24h
# `expires_at` the reminder worker puts on recent_outbound rows, which exists
# for the same reason: bounding how long stale context can be matched against
# an unrelated later message.
_ACTIVE_TASK_TTL = timedelta(hours=24)


class _Target(TypedDict):
    """The task a completion reply resolved to.

    needs_notion_completion is False for reminders, whose Notion page the
    reminder worker already set to Completed at delivery time.
    title is private — never log it.
    """
    source: Literal["reminder", "active_task"]
    page_id: str
    title: str
    work_type: str
    energy_required: str
    needs_notion_completion: bool
    signal_timestamp: int | None


def _active_task_age(active_task: ActiveTask, *, now: datetime) -> timedelta | None:
    """Return how long ago this active_task was selected, or None if unknown.

    An unknown age is treated as stale by the caller. Entries checkpointed
    before `selected_at` existed carry no age, and those are exactly the
    long-lived leftovers this guard exists to catch.
    """
    raw = active_task.get("selected_at")
    if not raw:
        return None
    try:
        selected_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if selected_at.tzinfo is None:
        selected_at = selected_at.replace(tzinfo=UTC)
    return now - selected_at


def _resolve_target(
    *,
    active_task: ActiveTask | None,
    reminder: AwaitingReply | None,
    now: datetime,
) -> _Target | None:
    """Pick the task a completion reply refers to, or None if it is ambiguous.

    Pure function — the DB and Notion calls live in the caller so this stays
    directly testable against the incident shape.
    """
    reminder_target: _Target | None = None
    if reminder and reminder.get("notion_page_id"):
        reminder_target = _Target(
            source="reminder",
            page_id=reminder["notion_page_id"],
            title=reminder.get("title", ""),
            work_type="",
            energy_required="",
            # Already Completed by the reminder worker at delivery time.
            needs_notion_completion=False,
            signal_timestamp=reminder.get("signal_timestamp"),
        )

    active_target: _Target | None = None
    active_age: timedelta | None = None
    if active_task and active_task.get("page_id"):
        active_age = _active_task_age(active_task, now=now)
        if active_age is not None and active_age <= _ACTIVE_TASK_TTL:
            active_target = _Target(
                source="active_task",
                page_id=active_task["page_id"],
                title=(active_task.get("title") or "").strip(),
                work_type=active_task.get("work_type", ""),
                energy_required=active_task.get("energy_required", ""),
                needs_notion_completion=True,
                signal_timestamp=None,
            )

    if reminder_target and active_target and reminder is not None and active_age is not None:
        # Both are live context. The user is answering whichever came last.
        reminder_sent_at = reminder["sent_at"]
        if reminder_sent_at.tzinfo is None:
            reminder_sent_at = reminder_sent_at.replace(tzinfo=UTC)
        reminder_age = now - reminder_sent_at
        return reminder_target if reminder_age <= active_age else active_target

    return reminder_target or active_target


async def complete_node(state: State) -> dict[str, Any]:
    """COMPLETE handler: resolve the finished task, update Notion, reward, reply."""
    peer = state.get("peer", "")

    try:
        from app.tools import notion, recent_outbound
        from app.tools.rewards import maybe_reward

        active_task = state.get("active_task")

        # The reminder lookup fails soft: if Postgres is unavailable we still
        # complete against active_task rather than erroring the turn. The
        # degradation is logged inside load_awaiting_reply().
        reminder = await recent_outbound.load_awaiting_reply(peer)

        now = datetime.now(UTC)
        target = _resolve_target(active_task=active_task, reminder=reminder, now=now)

        # Log the decision, not just the outcome. Previously the only record of
        # a completion was the page id acted on, with nothing to check it
        # against — which is why a wrong-target completion left no trace.
        # Page ids only; titles are private.
        log.info(
            "complete_node.target_resolved",
            peer=peer,
            source=target["source"] if target else None,
            page_id=target["page_id"] if target else None,
            had_reminder_candidate=reminder is not None,
            had_active_task_candidate=bool(active_task and active_task.get("page_id")),
            active_task_used=bool(target and target["source"] == "active_task"),
        )

        if target is None:
            # Fail closed. Completing "whatever is in the checkpoint" is how an
            # untouched task gets closed behind the user's back; asking costs a
            # single message and cannot corrupt the list.
            ambiguous_draft: OutboundDraft = {
                "recipient": peer,
                "body": "Nice one! Which task did you finish?",
                "notion_page_id": None,
            }
            return {
                "pending_outbound": [ambiguous_draft],
                # Drop the stale entry so it cannot win a later turn either.
                "active_task": None,
                "conversation_state": "idle",
            }

        page_id = target["page_id"]

        # Reminder pages are already Completed — the worker closes them at
        # delivery time (docs/user-interactions.md). Writing again would be a
        # redundant API call, not a correction.
        if target["needs_notion_completion"]:
            await notion.update_status(page_id, "Completed")

        streak = state.get("streak", 0) + 1
        tasks_today = state.get("tasks_completed_today", 0) + 1

        # Get reward result — text celebration + optional image path
        reward_result = await maybe_reward(
            peer=peer,
            task_title=target["title"],
            notion_page_id=page_id,
            streak=streak,
            work_type=target["work_type"],
            energy_required=target["energy_required"],
        )

        # Resolve the reminder row so a later "done" cannot match it again.
        if target["source"] == "reminder" and target["signal_timestamp"] is not None:
            await recent_outbound.clear_awaiting_reply(
                peer=peer,
                signal_timestamp=target["signal_timestamp"],
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

        log.info("complete_node.done", peer=peer, page_id=page_id, streak=streak)
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
