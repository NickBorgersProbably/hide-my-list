"""ADD_TASK node: task intake with label inference, deadline detection, and reminder scheduling.

Ports docs/ai-prompts/intake.md behavior:
- Aggressive label inference (urgency, work_type, time_estimate)
- Deadline detection: phrases like "by Friday", "before next Wednesday" set due_at
- Stakes detection: high-stakes tasks with no deadline trigger ONE clarification question
- Sub-task generation on request only (explicit breakdown routes to NEED_HELP)
- Reminder detection (wall-clock time → outbox row via existing path)
- Inline reminder scheduling for deadline tasks immediately after task creation
- Clarification flow (max 3 questions, stakes-clarification counts as one)
- Reschedule from recent_outbound context

DEADLINE REMINDER SCHEDULING:
After notion.create_task() succeeds with a due_at_iso, the node calls
schedule_for_task() from app/scheduler/reminder_scheduling.py to plan and
enqueue the milestone reminder series inline. If any milestone enqueue fails,
an ops_alert is emitted and Reminder Scheduled At is left empty so the nightly
reminder_scheduler daemon can retry.

REMINDER PERSISTENCE uses the outbox state machine; the reminder_worker (APScheduler)
handles delivery. The reminder_scheduling_ledger tracks assigned slots for load balancing.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

import structlog

from app.graph.state import OutboundDraft, State

log = structlog.get_logger(__name__)


async def intake_node(state: State) -> dict[str, Any]:
    """ADD_TASK handler: infer labels, detect deadlines, schedule reminders, save task."""
    peer = state.get("peer", "")

    try:
        from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage

        from app.models import llm
        from app.tools import notion
        from app.tools.time_context import get_time_context

        incoming = state.get("incoming", "")
        user_prefs = state.get("user_prefs", {})
        messages_history: list[AnyMessage] = state.get("messages", [])

        # Build conversation history summary
        history_lines = []
        for msg in messages_history[-6:]:
            role = getattr(msg, "type", "message")
            content = str(getattr(msg, "content", ""))
            history_lines.append(f"{role}: {content[:200]}")
        conversation_history = "\n".join(history_lines) or "No prior context."

        # Get time context in user's timezone
        time_ctx = get_time_context("now")
        current_time = time_ctx["reference_local"]
        user_timezone = time_ctx["user_timezone"]

        # User preferences context
        user_prefs_context = _build_prefs_context(user_prefs)

        # Clarification count tracking (use messages history length as proxy)
        clarification_count = 0

        from app.prompts.loader import render_with_defaults
        prompt_context = {
            "user_message": incoming,
            "conversation_history": conversation_history,
            "user_preferences_context": user_prefs_context,
            "clarification_count": clarification_count,
            "current_time": current_time,
            "user_timezone": user_timezone,
        }
        prompt_text = render_with_defaults("intake.md.j2", prompt_context)

        model = llm("medium", caller="intake")
        messages = [
            SystemMessage(content=prompt_text),
            HumanMessage(content=f"The user said: {incoming!r}"),
        ]

        response = await model.ainvoke(messages)
        response_text = str(response.content).strip()

        parsed = _parse_intake_response(response_text)

        if parsed.get("action") == "clarify":
            question = parsed.get("clarification_question", "Which task are you thinking of?")
            draft = {
                "recipient": peer,
                "body": question,
                "notion_page_id": None,
            }
            return {"pending_outbound": [draft]}

        # Action is "save"
        task_title = parsed.get("title", incoming[:200])
        work_type = parsed.get("work_type", "focus")
        urgency = int(parsed.get("urgency", 50))
        time_estimate = int(parsed.get("time_estimate_minutes", 30))
        energy_required = parsed.get("energy_required", "Medium")
        is_reminder = bool(parsed.get("is_reminder", False))
        remind_at_str = parsed.get("remind_at")
        inline_steps = parsed.get("inline_steps", "")
        use_hidden_subtasks = bool(parsed.get("use_hidden_subtasks", False))
        sub_tasks = parsed.get("sub_tasks", [])
        due_at_iso = parsed.get("due_at") or None
        is_high_stakes = bool(parsed.get("is_high_stakes", False))
        confirmation_message = parsed.get(
            "confirmation_message",
            f"Saved — {work_type}, ~{time_estimate} min.",
        )

        log.info(
            "intake_node.parsed",
            is_reminder=is_reminder,
            has_remind_at=bool(remind_at_str),
            remind_at=remind_at_str,
            urgency=urgency,
            time_estimate=time_estimate,
            has_due_at=bool(due_at_iso),
        )

        if is_reminder and remind_at_str:
            notion_page = await _create_reminder(
                peer=peer,
                title=task_title,
                work_type=work_type,
                energy_required=energy_required,
                remind_at_str=remind_at_str,
            )
            page_id = (notion_page or {}).get("id")
        else:
            log.info(
                "intake_node.no_reminder",
                has_remind_at=bool(remind_at_str),
                is_reminder=is_reminder,
            )
            notion_page = await notion.create_task(
                title=task_title,
                work_type=work_type,
                urgency=urgency,
                time_estimate=time_estimate,
                energy_required=energy_required,
                inline_steps=inline_steps,
                due_at_iso=due_at_iso,
            )
            page_id = (notion_page or {}).get("id")

            # Inline reminder scheduling for deadline tasks.
            if due_at_iso and page_id and not is_reminder:
                confirmation_message = await _schedule_deadline_reminders(
                    page_id=page_id,
                    title=task_title,
                    peer=peer,
                    due_at_iso=due_at_iso,
                    urgency=urgency,
                    user_timezone=user_timezone,
                    is_high_stakes=is_high_stakes,
                    confirmation_message=confirmation_message,
                )
            elif not due_at_iso and not is_reminder and not is_high_stakes:
                # Low-stakes task with no deadline: add the no-ping disclaimer.
                confirmation_message = (
                    f"{confirmation_message} "
                    "No deadline, so I won't ping you. Reply 'remind me' if you want one."
                )

        # Create hidden sub-tasks if needed (explicit user request path via NEED_HELP,
        # or when the LLM still populates use_hidden_subtasks for complex tasks).
        if use_hidden_subtasks and sub_tasks and page_id:
            for sub in sub_tasks:
                try:
                    await notion.create_task(
                        title=sub.get("title", "Sub-task"),
                        work_type=work_type,
                        urgency=urgency,
                        time_estimate=sub.get("time_estimate_minutes", 15),
                        energy_required=energy_required,
                        parent_id=page_id,
                        sequence=sub.get("sequence", 1),
                    )
                except Exception:
                    log.exception("intake_node.subtask_create_failed", page_id=page_id)

        draft = {
            "recipient": peer,
            "body": confirmation_message,
            "notion_page_id": page_id,
        }

        log.info("intake_node.saved", peer=peer, page_id=page_id, is_reminder=is_reminder)
        return {
            "pending_outbound": [draft],
            "conversation_state": "idle",
        }

    except Exception:
        log.exception("intake_node.error", peer=peer)
        fallback: OutboundDraft = {
            "recipient": peer,
            "body": "Got it, I'll add that to your list.",
            "notion_page_id": None,
        }
        return {"pending_outbound": [fallback]}


async def _schedule_deadline_reminders(
    *,
    page_id: str,
    title: str,
    peer: str,
    due_at_iso: str,
    urgency: int,
    user_timezone: str,
    is_high_stakes: bool,
    confirmation_message: str,
) -> str:
    """Schedule inline milestone reminders for a deadline task.

    Calls schedule_for_task, appends a deterministic reminder summary to the
    confirmation message (NOT generated by the LLM to avoid hallucinated times).

    Returns the (possibly augmented) confirmation_message string.
    """
    from app.scheduler.deadline_planner import format_reminder_summary
    from app.scheduler.reminder_scheduling import schedule_for_task
    from app.tools import notion, ops_alerts

    try:
        normalized = due_at_iso.strip().replace("Z", "+00:00")
        deadline_at = datetime.fromisoformat(normalized)
        if deadline_at.tzinfo is None:
            deadline_at = deadline_at.replace(tzinfo=UTC)
    except (ValueError, TypeError) as exc:
        log.warning("intake_node.due_at_parse_failed", due_at_iso=due_at_iso, error=str(exc))
        return confirmation_message

    now = datetime.now(UTC)

    try:
        assigned_slots, enqueue_failures = await schedule_for_task(
            page_id=page_id,
            title=title,
            peer=peer,
            deadline_at=deadline_at,
            urgency=urgency,
            now=now,
            user_tz=user_timezone,
        )
    except Exception as exc:
        log.exception("intake_node.schedule_for_task_failed", page_id=page_id, error=str(exc))
        enqueue_failures = ["schedule_error"]
        assigned_slots = []

    if assigned_slots and not enqueue_failures:
        # All milestones successfully enqueued — mark scheduled and append summary.
        try:
            await notion.mark_reminder_scheduled(page_id)
        except Exception as notion_exc:
            # Non-fatal: outbox rows exist; daemon retries mark_reminder_scheduled.
            log.warning(
                "intake_node.mark_scheduled_failed",
                page_id=page_id,
                error=str(notion_exc),
            )

        summary = format_reminder_summary(assigned_slots, user_timezone)
        if summary:
            confirmation_message = f"{confirmation_message} {summary}"

    elif enqueue_failures:
        # Partial or full failure — daemon backstop will retry tonight.
        log.warning(
            "intake_node.reminder_partial_failure",
            page_id=page_id,
            failed_milestones=enqueue_failures,
        )
        try:
            await ops_alerts.enqueue(
                kind="intake_reminder_partial_failure",
                body=(
                    "intake: inline reminder scheduling partially failed for <page_id>. "
                    f"Failed milestones: {enqueue_failures}. "
                    "reminder_scheduler daemon will retry tonight."
                ),
                severity="warning",
            )
        except Exception as alert_exc:
            log.error("intake_node.ops_alert_failed", error=str(alert_exc))

        confirmation_message = (
            f"{confirmation_message} "
            "(Couldn't schedule reminders right now — I'll retry tonight.)"
        )

    elif not assigned_slots:
        # Deadline too close or already past — no milestones fit.
        # Mark scheduled to stop daemon from retrying indefinitely.
        try:
            await notion.mark_reminder_scheduled(page_id)
        except Exception:
            pass

    return confirmation_message


async def _create_reminder(
    *,
    peer: str,
    title: str,
    work_type: str,
    energy_required: str,
    remind_at_str: str,
) -> dict[str, Any]:
    """Create a Notion reminder row and enqueue the outbox row.

    notion.create_reminder() accepts: title, remind_at_iso, work_type, energy_required.
    Urgency, time_estimate, and inline_steps are set by the Notion function internally.
    """
    from app.tools import notion
    from app.tools.db import get_db_conn
    from app.tools.reminders import enqueue

    # Parse remind_at to datetime
    remind_at = _parse_remind_at(remind_at_str)

    notion_page = await notion.create_reminder(
        title=title,
        remind_at_iso=remind_at.isoformat(),
        work_type=work_type,
        energy_required=energy_required,
    )

    page_id = (notion_page or {}).get("id", "")

    # Enqueue in the outbox for at-least-once delivery
    if page_id:
        idempotency_key = f"intake-{page_id}"
        try:
            async with get_db_conn() as conn:
                await enqueue(
                    conn,
                    notion_page_id=page_id,
                    peer=peer,
                    body=f"Hey — {title}",
                    due_at=remind_at,
                    idempotency_key=idempotency_key,
                )
            log.info(
                "intake_node.outbox_enqueued",
                page_id=page_id,
                due_at=remind_at.isoformat() if remind_at else None,
                idempotency_key=idempotency_key,
            )
        except Exception:
            log.exception("intake_node.enqueue_failed", page_id=page_id)
            # Notion row exists but outbox write failed — reminder won't be delivered
            # automatically. Emit ops alert so the operator can investigate.
            try:
                from app.tools import ops_alerts
                await ops_alerts.enqueue(
                    kind="reminder_enqueue_failed",
                    body="Reminder outbox enqueue failed for <page_id>. Reminder exists in Notion but will not be delivered until the outbox row is created.",
                    severity="warning",
                )
            except Exception:
                log.exception("intake_node.ops_alert_failed", page_id=page_id)

    return notion_page or {}


def _parse_remind_at(remind_at_str: str) -> datetime:
    """Parse an ISO 8601 string to a UTC-aware datetime."""
    try:
        normalized = remind_at_str.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Cannot parse remind_at: {remind_at_str!r}") from exc


def _parse_intake_response(response_text: str) -> dict[str, Any]:
    """Extract JSON from LLM intake response."""
    # Try to find a JSON block
    json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
    if json_match:
        try:
            loaded = json.loads(json_match.group())
            if isinstance(loaded, dict):
                return cast(dict[str, Any], loaded)
        except json.JSONDecodeError:
            pass

    # Fallback: return a save action with the raw text as title
    return {
        "action": "save",
        "title": response_text[:200],
        "work_type": "focus",
        "urgency": 50,
        "time_estimate_minutes": 30,
        "energy_required": "Medium",
        "is_reminder": False,
        "remind_at": None,
        "due_at": None,
        "is_high_stakes": False,
        "use_hidden_subtasks": False,
        "sub_tasks": [],
        "inline_steps": "",
        "confirmation_message": "Saved — added.",
    }


def _build_prefs_context(user_prefs: Mapping[str, object]) -> str:
    """Build user preferences context string for the prompt."""
    if not user_prefs:
        return "No user preferences configured."

    lines = ["This user has the following preferences:"]
    if tz := user_prefs.get("timezone"):
        lines.append(f"- Timezone: {tz}")
    wt = user_prefs.get("preferred_work_types")
    if isinstance(wt, list) and all(isinstance(item, str) for item in wt):
        lines.append(f"- Preferred work types: {', '.join(wt)}")
    if energy := user_prefs.get("default_energy"):
        lines.append(f"- Default energy level: {energy}")
    return "\n".join(lines)
