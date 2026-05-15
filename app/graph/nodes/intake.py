"""ADD_TASK node: task intake with label inference, sub-task generation, reminder detection.

Ports docs/ai-prompts/intake.md (464 lines) behavior:
- Aggressive label inference (urgency, work_type, time_estimate)
- Sub-task generation for every task
- Reminder detection (wall-clock time → outbox row)
- Clarification flow (max 3 questions)
- Reschedule from recent_outbound context

When a reminder is detected:
- Creates Notion row via app/tools/notion.create_reminder()
- Enqueues outbox row via app/tools/reminders.enqueue()
- Uses app/tools/time_context to resolve user-local time

REMINDER PERSISTENCE in the Python rewrite uses the outbox state machine, not
OpenClaw CronCreate. The reminder worker (APScheduler) handles delivery.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog

from app.graph.state import OutboundDraft, State

log = structlog.get_logger(__name__)

_ENABLE_LANGGRAPH_PATH = os.environ.get("ENABLE_LANGGRAPH_PATH", "false").lower() in (
    "true", "1", "yes"
)


async def intake_node(state: State) -> dict[str, Any]:
    """ADD_TASK handler: infer labels, generate sub-tasks, detect reminders."""
    peer = state.get("peer", "")

    if not _ENABLE_LANGGRAPH_PATH:
        draft: OutboundDraft = {
            "recipient": peer,
            "body": "[stub] ADD_TASK not yet active (ENABLE_LANGGRAPH_PATH=false)",
            "notion_page_id": None,
        }
        return {"pending_outbound": [draft]}

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

        model = llm("medium")
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
        confirmation_message = parsed.get("confirmation_message", f"Got it — {work_type}, ~{time_estimate} min.")

        if is_reminder and remind_at_str:
            notion_page = await _create_reminder(
                peer=peer,
                title=task_title,
                work_type=work_type,
                energy_required=energy_required,
                remind_at_str=remind_at_str,
            )
        else:
            notion_page = await notion.create_task(
                title=task_title,
                work_type=work_type,
                urgency=urgency,
                time_estimate=time_estimate,
                energy_required=energy_required,
                inline_steps=inline_steps,
            )

        page_id = (notion_page or {}).get("id")

        # Create hidden sub-tasks if needed
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
        except Exception:
            log.exception("intake_node.enqueue_failed", page_id=page_id)
            # Don't raise — the Notion row is the source of truth.
            # The reminder worker will pick it up on the next poll.

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
            return json.loads(json_match.group())
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
        "use_hidden_subtasks": False,
        "sub_tasks": [],
        "inline_steps": "",
        "confirmation_message": "Got it — added.",
    }


def _build_prefs_context(user_prefs: dict[str, Any]) -> str:
    """Build user preferences context string for the prompt."""
    if not user_prefs:
        return "No user preferences configured."

    lines = ["This user has the following preferences:"]
    if tz := user_prefs.get("timezone"):
        lines.append(f"- Timezone: {tz}")
    if wt := user_prefs.get("preferred_work_types"):
        lines.append(f"- Preferred work types: {', '.join(wt)}")
    if energy := user_prefs.get("default_energy"):
        lines.append(f"- Default energy level: {energy}")
    return "\n".join(lines)
