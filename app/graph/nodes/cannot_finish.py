"""CANNOT_FINISH node: shame-safe breakdown handling.

When the user indicates they can't finish a task, gathers progress info and
creates sub-tasks for the remaining work.

Implements docs/ai-prompts/cannot-finish.md behavior.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import structlog

from app.graph.state import OutboundDraft, State

log = structlog.get_logger(__name__)

_ENABLE_LANGGRAPH_PATH = os.environ.get("ENABLE_LANGGRAPH_PATH", "false").lower() in (
    "true", "1", "yes"
)

_CANNOT_FINISH_SYSTEM_PROMPT = """\
The user indicates they cannot finish the current task. Gather progress and break down remaining work.

CURRENT TASK: {task_title}
ORIGINAL TIME ESTIMATE: {time_estimate} minutes
USER MESSAGE: "{user_message}"

STEP 1: Ask what was accomplished
Generate a brief, friendly question to understand their progress.

STEP 2: Once progress is described, analyze remaining work
- What did the user complete?
- What specific work remains?
- How can remaining work be broken into 15-90 minute chunks?

STEP 3: Create sub-tasks for remaining work
- Each sub-task must be specific and actionable
- First sub-task should be the immediate next step
- Sub-tasks are hidden from user

SHAME PREVENTION (MANDATORY):
- Never imply the user has failed, fallen short, or should have done better
- Never use "you didn't", "you should have", "you forgot", or "you failed"
- Frame this as progress: "Cannot finish = now we know more about this task"
- Lead with what they accomplished, not what's left

PROGRESS QUESTION TEMPLATES (shame-safe):
- "No worries — you figured out it's bigger than it seemed. What did you get into?"
- "Got it — you made real progress. What part did you get through?"
- "That's totally fine — now we know more about this task. Where'd you get to?"
- "Totally understand — this clearly needed to be broken down more. Tell me what you accomplished."

OUTPUT (JSON):
{{
  "phase": "ask_progress",
  "user_message": "...",
  "progress_question": "..."
}}

Or after progress described:
{{
  "phase": "analyze_remaining",
  "user_message": "...",
  "completed_portion": "...",
  "remaining_sub_tasks": [
    {{"title": "...", "time_estimate_minutes": 0, "sequence": 1}}
  ],
  "next_sub_task_message": "..."
}}
"""


async def cannot_finish_node(state: State) -> dict[str, Any]:
    """CANNOT_FINISH handler: gather progress and break down remaining work."""
    peer = state.get("peer", "")

    if not _ENABLE_LANGGRAPH_PATH:
        draft: OutboundDraft = {
            "recipient": peer,
            "body": "[stub] CANNOT_FINISH not yet active (ENABLE_LANGGRAPH_PATH=false)",
            "notion_page_id": None,
        }
        return {"pending_outbound": [draft]}

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.models import llm
        from app.tools import notion
        from app.prompts.loader import render_with_defaults

        incoming = state.get("incoming", "")
        active_task = state.get("active_task")

        task_title = active_task.get("title", "your task") if active_task else "your task"
        page_id = active_task.get("page_id", "") if active_task else ""
        time_estimate = active_task.get("time_estimate", 30) if active_task else 30

        prompt_text = render_with_defaults(
            "cannot_finish.md.j2",
            {
                "task_title": task_title,
                "time_estimate": time_estimate,
                "user_message": incoming,
            },
            defaults={
                "task_title": "your task",
                "time_estimate": 30,
                "user_message": "",
            },
        )

        model = llm("medium")
        messages = [
            SystemMessage(content=prompt_text),
            HumanMessage(content=incoming),
        ]

        response = await model.ainvoke(messages)
        response_text = str(response.content).strip()

        user_message = _parse_cannot_finish_response(response_text)

        draft = {
            "recipient": peer,
            "body": user_message,
            "notion_page_id": page_id,
        }

        log.info("cannot_finish_node.response", peer=peer, page_id=page_id)
        return {
            "pending_outbound": [draft],
            "conversation_state": "active",
        }

    except Exception:
        log.exception("cannot_finish_node.error", peer=peer)
        fallback: OutboundDraft = {
            "recipient": peer,
            "body": "No worries — that task was bigger than it looked. What did you get into before stopping?",
            "notion_page_id": None,
        }
        return {"pending_outbound": [fallback]}


def _parse_cannot_finish_response(response_text: str) -> str:
    """Extract user_message from LLM JSON response."""
    json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group())
            msg = data.get("user_message") or data.get("progress_question") or data.get("next_sub_task_message")
            if msg:
                return str(msg)
        except json.JSONDecodeError:
            pass
    return response_text[:300] if response_text else "No worries — what did you get into before stopping?"
