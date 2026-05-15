"""GET_TASK node: task selection using scoring algorithm.

Reads pending tasks from Notion, scores them per docs/ai-prompts/selection.md,
and drafts a suggestion into pending_outbound.

Uses expensive-tier LLM for nuanced scoring and suggestion text.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

import structlog

from app.graph.state import OutboundDraft, State

log = structlog.get_logger(__name__)

_ENABLE_LANGGRAPH_PATH = os.environ.get("ENABLE_LANGGRAPH_PATH", "false").lower() in (
    "true", "1", "yes"
)


def _mood_to_work_type(mood: str | None) -> str:
    """Map mood string to preferred work type."""
    if not mood:
        return "any"
    mood_lower = mood.lower()
    if any(w in mood_lower for w in ("focus", "sharp", "concentrate")):
        return "focus"
    if any(w in mood_lower for w in ("creative", "inspired", "imaginative")):
        return "creative"
    if any(w in mood_lower for w in ("social", "energetic", "talkative")):
        return "social"
    if any(w in mood_lower for w in ("tired", "low", "exhausted", "slow")):
        return "independent"
    return "any"


def _time_of_day() -> str:
    """Return a string label for the current UTC hour."""
    hour = datetime.now(UTC).hour
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"


async def selection_node(state: State) -> dict[str, Any]:
    """GET_TASK handler: score and suggest a task.

    When ENABLE_LANGGRAPH_PATH=false, echoes a stub. When true, queries Notion,
    scores tasks with the LLM, and populates pending_outbound.
    """
    peer = state.get("peer", "")

    if not _ENABLE_LANGGRAPH_PATH:
        draft: OutboundDraft = {
            "recipient": peer,
            "body": "[stub] GET_TASK not yet active (ENABLE_LANGGRAPH_PATH=false)",
            "notion_page_id": None,
        }
        return {"pending_outbound": [draft]}

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.models import llm
        from app.tools import notion

        available_minutes = state.get("available_minutes") or 30
        mood = state.get("mood")
        preferred_work_type = _mood_to_work_type(mood)
        time_of_day = _time_of_day()

        # Fetch pending tasks from Notion
        tasks_raw = await notion.query_pending()
        tasks = tasks_raw.get("results", [])

        # Build simplified task list for the prompt
        simplified: list[dict] = []
        for task in tasks:
            props = task.get("properties", {})
            simplified.append({
                "id": task.get("id", ""),
                "title": _extract_title(props),
                "work_type": _extract_select(props, "Work Type"),
                "urgency": _extract_number(props, "Urgency", 50),
                "time_estimate": _extract_number(props, "Time Estimate (min)", 30),
                "energy_required": _extract_select(props, "Energy Required"),
                "rejection_count": _extract_number(props, "Rejection Count", 0),
            })

        tasks_json = json.dumps(simplified, indent=2)

        # Load and render the selection prompt
        from app.prompts.loader import render_with_defaults
        prompt_context = {
            "available_minutes": available_minutes,
            "mood": mood or "neutral",
            "preferred_work_type": preferred_work_type,
            "time_of_day": time_of_day,
            "tasks_json": tasks_json,
        }
        prompt_text = render_with_defaults("selection.md.j2", prompt_context)

        model = llm("expensive")
        messages = [
            SystemMessage(content=prompt_text),
            HumanMessage(content="Select the best task for me right now."),
        ]

        response = await model.ainvoke(messages)
        response_text = str(response.content).strip()

        # Parse JSON response from LLM
        user_message, selected_page_id = _parse_selection_response(response_text, peer)

        draft = {
            "recipient": peer,
            "body": user_message,
            "notion_page_id": selected_page_id,
        }

        log.info("selection_node.suggestion", peer=peer, notion_page_id=selected_page_id)
        return {
            "pending_outbound": [draft],
            "conversation_state": "selection",
        }

    except Exception:
        log.exception("selection_node.error", peer=peer)
        fallback: OutboundDraft = {
            "recipient": peer,
            "body": "Having trouble finding a task right now. Try again in a moment?",
            "notion_page_id": None,
        }
        return {"pending_outbound": [fallback]}


def _extract_title(props: dict[str, Any]) -> str:
    """Extract the title string from a Notion page properties dict."""
    title_prop = props.get("Title", {})
    items = title_prop.get("title", [])
    return "".join(item.get("plain_text", "") for item in items)


def _extract_select(props: dict[str, Any], key: str) -> str:
    """Extract a select property value."""
    sel = props.get(key, {}).get("select") or {}
    return sel.get("name", "")


def _extract_number(props: dict[str, Any], key: str, default: int = 0) -> int:
    """Extract a number property value."""
    num = props.get(key, {}).get("number")
    if num is None:
        return default
    return int(num)


def _parse_selection_response(response_text: str, peer: str) -> tuple[str, str | None]:
    """Parse the LLM's JSON selection response. Returns (user_message, page_id)."""
    import re

    # Try to extract JSON block
    json_match = re.search(r"\{[^{}]+\}", response_text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group())
            user_message = data.get("user_message", "")
            selected_id = data.get("selected_task_id") or None
            if user_message:
                return user_message, selected_id
        except json.JSONDecodeError:
            pass

    # Fallback: use the raw response text as the user message
    user_message = response_text[:500] if response_text else "No suitable task found right now."
    return user_message, None
