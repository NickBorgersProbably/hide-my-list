"""CHECK_IN node: system-initiated progress follow-up.

Triggered by the APScheduler check_in_dispatcher job, never by user messages.
Guards against accidental user-message routing (classify_intent filters this).

Implements docs/ai-prompts/check-in.md behavior.
"""
from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from typing import Any

import structlog

from app.graph.state import OutboundDraft, State

log = structlog.get_logger(__name__)

_ENABLE_LANGGRAPH_PATH = os.environ.get("ENABLE_LANGGRAPH_PATH", "false").lower() in (
    "true", "1", "yes"
)

_CHECK_IN_SYSTEM_PROMPT = """\
The user accepted a task but the expected completion time has passed.
Check in on their progress.

ACTIVE TASK: {task_title}
TIME ESTIMATE: {time_estimate} minutes
TIME ELAPSED: {elapsed_minutes} minutes
CHECK_IN_COUNT: {check_in_count}

Generate a brief, friendly check-in message. Keep it casual and non-judgmental.
The user may have:
- Completed the task and forgot to say so
- Still be working on it
- Gotten distracted
- Needed more time than estimated

SHAME PREVENTION (MANDATORY):
- Never imply the user has failed, fallen short, or should have done better
- Tone: "friend checking in," not "manager following up"
- Check-ins = potential shame trigger — keep them curious and warm

TEMPLATES BY CHECK_IN_COUNT:
- 0: "How's the [task] going? Still at it?"
- 1: "Just checking in — how are you getting on with [task]?"
- 2: "Hey, no pressure — still working on [task], or want to take a break?"

OUTPUT (JSON):
{{
  "check_in_message": "..."
}}
"""


async def check_in_node(state: State) -> dict[str, Any]:
    """CHECK_IN handler: system-initiated progress follow-up.

    Only invoked by the check_in_dispatcher APScheduler job, not by user messages.
    If routed here from a user message (e.g., a misclassification), the node
    logs a warning and routes to chat fallback behavior.
    """
    peer = state.get("peer", "")

    if not _ENABLE_LANGGRAPH_PATH:
        draft: OutboundDraft = {
            "recipient": peer,
            "body": "[stub] CHECK_IN not yet active (ENABLE_LANGGRAPH_PATH=false)",
            "notion_page_id": None,
        }
        return {"pending_outbound": [draft]}

    # Guard: if no active task, skip check-in
    active_task = state.get("active_task")
    if not active_task:
        log.info("check_in_node.skip_no_active_task", peer=peer)
        return {"conversation_state": "idle"}

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.models import llm
        from app.prompts.loader import render_with_defaults

        task_title = active_task.get("title", "your task")
        time_estimate = active_task.get("time_estimate", 30)
        page_id = active_task.get("page_id", "")

        # Calculate elapsed minutes (using started_at if available in active_task)
        started_at_str = active_task.get("started_at")
        if started_at_str:
            try:
                started_at = datetime.fromisoformat(started_at_str)
                elapsed = int((datetime.now(UTC) - started_at).total_seconds() / 60)
            except (ValueError, TypeError):
                elapsed = time_estimate
        else:
            elapsed = time_estimate

        check_in_count = active_task.get("check_in_count", 0)

        prompt_text = render_with_defaults(
            "check_in.md.j2",
            {
                "task_title": task_title,
                "time_estimate": time_estimate,
                "elapsed_minutes": elapsed,
                "check_in_count": check_in_count,
            },
            defaults={
                "task_title": "your task",
                "time_estimate": 30,
                "elapsed_minutes": 30,
                "check_in_count": 0,
            },
        )

        model = llm("medium")
        messages = [
            SystemMessage(content=prompt_text),
            HumanMessage(content="Generate a check-in message."),
        ]

        response = await model.ainvoke(messages)
        response_text = str(response.content).strip()

        user_message = _parse_check_in_response(response_text)

        draft = {
            "recipient": peer,
            "body": user_message,
            "notion_page_id": page_id,
        }

        # Update check_in_count and check_in_due_at in active_task
        new_count = check_in_count + 1
        updated_active_task = dict(active_task)
        updated_active_task["check_in_count"] = new_count

        log.info("check_in_node.sent", peer=peer, check_in_count=new_count)
        return {
            "pending_outbound": [draft],
            "active_task": updated_active_task,
            "conversation_state": "checking_in",
        }

    except Exception:
        log.exception("check_in_node.error", peer=peer)
        fallback: OutboundDraft = {
            "recipient": peer,
            "body": "Hey, how's that task going?",
            "notion_page_id": None,
        }
        return {"pending_outbound": [fallback]}


def _parse_check_in_response(response_text: str) -> str:
    """Extract check_in_message from LLM JSON response."""
    json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group())
            msg = data.get("check_in_message")
            if msg:
                return str(msg)
        except json.JSONDecodeError:
            pass
    return response_text[:300] if response_text else "Hey, how's that task going?"
