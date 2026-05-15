"""NEED_HELP node: breakdown assistance.

When the user needs help starting or continuing a task, provides specific,
actionable guidance matched to their confidence level.

Implements docs/ai-prompts/breakdown.md behavior.
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

_NEED_HELP_SYSTEM_PROMPT = """\
The user needs help with their current task. Provide specific, actionable guidance.

CURRENT TASK: {task_title}
TASK SUB-STEPS: {inline_steps}
USER MESSAGE: "{user_message}"

ASSISTANCE PHILOSOPHY:
- Users avoid vague goals because they feel infinite
- Concrete, specific actions feel achievable
- The smaller the first step, the easier to start
- Always know what "done" looks like for each step

RESPONSE LEVELS (choose based on user signals):
1. OVERVIEW: List all steps with time estimates (confident user)
2. CURRENT_STEP: Focus on just the next step (uncertain user)
3. MICRO_ACTION: Provide the tiniest possible first action (stuck user)
4. HAND_HOLDING: Extremely detailed, click-by-click guidance (very stuck)

USER SIGNAL DETECTION:
- Confident: "What are the steps?", "Walk me through it"
- Uncertain: "I guess", hesitation, qualified acceptance
- Stuck: "I'm stuck", "I don't know where to start"
- Very stuck: Repeated help requests, frustration signals

SHAME PREVENTION (MANDATORY):
- Never imply the user has failed, fallen short, or should have done better
- Never use "you didn't", "you should have", "you forgot", or "you failed"
- "Stuck" is completely normal — acknowledge it warmly, then give a tiny action
- Frame confusion as information: "Now we know where to focus"

TEMPLATES:
- Overview: "Here's the plan: 1) X (5 min), 2) Y (10 min), 3) Z (5 min). Ready to start with X?"
- Current step: "Right now, focus on just this: [specific action]. That's it for now."
- Micro-action: "Don't worry about the whole thing. Just do this one tiny thing: [micro-action]"
- Hand-holding: "Here's exactly what to do: Open [app]. Click [button]. Type [specific text]. Done!"

OUTPUT (JSON):
{{
  "detected_confidence": "confident|uncertain|stuck|very_stuck",
  "response_level": "overview|current_step|micro_action|hand_holding",
  "immediate_action": "the very next thing to do right now",
  "user_message": "conversational response with appropriate detail level",
  "encouragement": "optional brief encouragement if user seems stuck"
}}
"""


async def need_help_node(state: State) -> dict[str, Any]:
    """NEED_HELP handler: provide actionable breakdown guidance."""
    peer = state.get("peer", "")

    if not _ENABLE_LANGGRAPH_PATH:
        draft: OutboundDraft = {
            "recipient": peer,
            "body": "[stub] NEED_HELP not yet active (ENABLE_LANGGRAPH_PATH=false)",
            "notion_page_id": None,
        }
        return {"pending_outbound": [draft]}

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.models import llm
        from app.prompts.loader import render_with_defaults

        incoming = state.get("incoming", "")
        active_task = state.get("active_task")

        if not active_task:
            draft = {
                "recipient": peer,
                "body": "Let's get you a task first! How much time do you have?",
                "notion_page_id": None,
            }
            return {"pending_outbound": [draft]}

        task_title = active_task.get("title", "your task")
        page_id = active_task.get("page_id", "")
        # inline_steps may be stored in active_task or fetched from Notion
        inline_steps = active_task.get("inline_steps", "No steps recorded yet.")

        prompt_text = render_with_defaults(
            "need_help.md.j2",
            {
                "task_title": task_title,
                "inline_steps": inline_steps,
                "user_message": incoming,
            },
            defaults={
                "task_title": "your task",
                "inline_steps": "No steps available.",
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

        user_message = _parse_need_help_response(response_text)

        draft = {
            "recipient": peer,
            "body": user_message,
            "notion_page_id": page_id,
        }

        log.info("need_help_node.response", peer=peer)
        return {"pending_outbound": [draft]}

    except Exception:
        log.exception("need_help_node.error", peer=peer)
        fallback: OutboundDraft = {
            "recipient": peer,
            "body": "Let's make this tiny. What's the very first physical thing you need to do?",
            "notion_page_id": None,
        }
        return {"pending_outbound": [fallback]}


def _parse_need_help_response(response_text: str) -> str:
    """Extract user_message from LLM JSON response."""
    json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group())
            msg = data.get("user_message")
            if msg:
                return str(msg)
        except json.JSONDecodeError:
            pass
    return response_text[:400] if response_text else "Let's break this down step by step."
