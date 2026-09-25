"""CHAT node: friendly fallback for unclassified or general messages.

Uses medium-tier LLM to provide brief, helpful responses.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from app.graph.context import render_history, render_recent_tasks
from app.graph.state import OutboundDraft, State

log = structlog.get_logger(__name__)

async def chat_node(state: State) -> dict[str, Any]:
    """CHAT handler: general conversation fallback using medium-tier LLM."""
    peer = state.get("peer", "")
    incoming = state.get("incoming", "")

    try:
        from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage

        from app.models import llm
        from app.prompts.loader import render_with_defaults

        messages_history: list[AnyMessage] = state.get("messages", [])
        conversation_context = render_history(messages_history)
        # The ledger is what lets "what task?" be answered after a reply that
        # did not repeat the title; the active task covers "is that mine now?".
        recent_tasks = render_recent_tasks(state.get("recent_tasks"), now=datetime.now(UTC))
        active_task = state.get("active_task") or {}
        active_task_title = (active_task.get("title") or "").strip() or "None"

        prompt_text = render_with_defaults(
            "chat.md.j2",
            {
                "user_message": incoming,
                "conversation_context": conversation_context,
                "recent_tasks": recent_tasks,
                "active_task_title": active_task_title,
            },
        )

        model = llm("medium", caller="chat")
        messages = [
            SystemMessage(content=prompt_text),
            HumanMessage(content=incoming),
        ]

        response = await model.ainvoke(messages)
        response_text = str(response.content).strip()

        # Truncate to 500 chars as a safety measure
        if len(response_text) > 500:
            response_text = response_text[:497] + "..."

        draft = {
            "recipient": peer,
            "body": response_text,
            "notion_page_id": None,
        }

        log.info("chat_node.response", peer=peer)
        return {"pending_outbound": [draft]}

    except Exception:
        log.exception("chat_node.error", peer=peer)
        fallback: OutboundDraft = {
            "recipient": peer,
            "body": "Having trouble thinking right now — try again?",
            "notion_page_id": None,
        }
        return {"pending_outbound": [fallback]}
