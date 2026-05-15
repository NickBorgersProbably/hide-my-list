"""REJECT node: shame-safe rejection handling.

When the user rejects a suggested task, classifies the reason, updates
rejection count in Notion, and suggests an alternative.

Implements docs/ai-prompts/rejection.md behavior.
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

_REJECTION_SYSTEM_PROMPT = """\
The user rejected the suggested task. Understand why and find an alternative.

REJECTED TASK: {task_title}
USER'S REASON: "{rejection_reason}"
REMAINING TASKS: {remaining_tasks_json}
USER CONTEXT: {available_minutes} minutes, {mood} mood

REJECTION CATEGORIES:
1. timing - "takes too long", "not enough time"
2. mood_mismatch - "not in the mood", "too tired for that"
3. blocked - "waiting on something", "can't do it yet"
4. already_done - "already did that", "finished already"
5. general - "just not feeling it", vague rejection

ACTIONS BY CATEGORY:
- timing: Suggest shorter task, note time preference
- mood_mismatch: Suggest different work type, avoid this type now
- blocked: Mark as blocked, don't suggest until unblocked
- already_done: Mark as completed, celebrate!
- general: Log rejection, try very different task

SHAME PREVENTION (MANDATORY):
- Never imply the user has failed, fallen short, or should have done better
- Never use "you didn't", "you should have", "you forgot", or "you failed"
- Rejection is the user helping you suggest better — say so explicitly
- Frame all difficulties as information, not shortcomings

Response templates:
- timing: "Got it — that one's too long right now. How about [shorter task]?"
- mood_mismatch: "Fair enough — that tells me what kind of work fits right now. How about [task]?"
- blocked: "I'll hold off on that one. In the meantime, try [task]?"
- already_done: "Oh nice, already done! Let me mark that off. Ready for another?"
- general: "No problem — that helps me learn what works for you. Here's something different: [task]?"

OUTPUT (JSON):
{{
  "rejection_category": "...",
  "task_update": {{
    "rejection_count_increment": 1,
    "rejection_note": "[timestamp] {reason}"
  }},
  "alternative_task_id": "..." or null,
  "user_message": "conversational response with alternative"
}}
"""


async def rejection_node(state: State) -> dict[str, Any]:
    """REJECT handler: classify rejection and suggest alternative."""
    peer = state.get("peer", "")

    if not _ENABLE_LANGGRAPH_PATH:
        draft: OutboundDraft = {
            "recipient": peer,
            "body": "[stub] REJECT not yet active (ENABLE_LANGGRAPH_PATH=false)",
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
        available_minutes = state.get("available_minutes") or 30
        mood = state.get("mood") or "neutral"

        task_title = active_task.get("title", "the suggested task") if active_task else "the suggested task"
        rejected_page_id = active_task.get("page_id", "") if active_task else ""

        # Fetch remaining tasks for alternative suggestion
        tasks_raw = await notion.query_pending()
        tasks = tasks_raw.get("results", [])
        remaining = [
            {
                "id": t.get("id", ""),
                "title": _extract_title(t.get("properties", {})),
                "work_type": _extract_select(t.get("properties", {}), "Work Type"),
                "time_estimate": _extract_number(t.get("properties", {}), "Time Estimate (min)", 30),
            }
            for t in tasks
            if t.get("id", "") != rejected_page_id
        ]

        # Load rejection prompt
        prompt_text = render_with_defaults(
            "rejection.md.j2",
            {
                "task_title": task_title,
                "rejection_reason": incoming,
                "remaining_tasks_json": json.dumps(remaining[:10], indent=2),
                "available_minutes": available_minutes,
                "mood": mood,
            },
            defaults={
                "task_title": "the suggested task",
                "rejection_reason": "",
                "remaining_tasks_json": "[]",
                "available_minutes": 30,
                "mood": "neutral",
            },
        )

        model = llm("medium")
        messages = [
            SystemMessage(content=prompt_text),
            HumanMessage(content=f"The user said: {incoming!r}"),
        ]

        response = await model.ainvoke(messages)
        response_text = str(response.content).strip()

        user_message, alternative_id = _parse_rejection_response(response_text)

        # Update rejection count in Notion
        if rejected_page_id:
            try:
                await notion.update_property(
                    rejected_page_id,
                    "Rejection Count",
                    {"number": (active_task.get("urgency", 0) if active_task else 0)},
                )
            except Exception:
                log.exception("rejection_node.notion_update_failed", page_id=rejected_page_id)

        draft = {
            "recipient": peer,
            "body": user_message,
            "notion_page_id": alternative_id,
        }

        log.info("rejection_node.alternative", peer=peer, alternative_id=alternative_id)
        return {
            "pending_outbound": [draft],
            "active_task": None,
            "conversation_state": "selection",
        }

    except Exception:
        log.exception("rejection_node.error", peer=peer)
        fallback: OutboundDraft = {
            "recipient": peer,
            "body": "No problem — that helps me learn. Want me to find something different?",
            "notion_page_id": None,
        }
        return {"pending_outbound": [fallback]}


def _parse_rejection_response(response_text: str) -> tuple[str, str | None]:
    """Parse LLM JSON response. Returns (user_message, alternative_task_id)."""
    json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group())
            return data.get("user_message", response_text[:300]), data.get("alternative_task_id")
        except json.JSONDecodeError:
            pass
    return response_text[:300] if response_text else "No problem. Want something different?", None


def _extract_title(props: dict[str, Any]) -> str:
    items = props.get("Title", {}).get("title", [])
    return "".join(item.get("plain_text", "") for item in items)


def _extract_select(props: dict[str, Any], key: str) -> str:
    sel = props.get(key, {}).get("select") or {}
    return sel.get("name", "")


def _extract_number(props: dict[str, Any], key: str, default: int = 0) -> int:
    num = props.get(key, {}).get("number")
    return int(num) if num is not None else default
