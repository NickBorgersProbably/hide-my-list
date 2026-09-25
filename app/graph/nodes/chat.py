"""CHAT node: friendly fallback for unclassified or general messages.

Uses medium-tier LLM to provide brief, helpful responses.

One path is deterministic and runs before the model: a short affirmative
("sure", "ok, that one") answering a fresh `suggested` ledger entry while no
task is active accepts that suggestion. A rejection alternative stays Pending
until the user accepts it, so this is where it is marked In Progress and
becomes the active task.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from app.graph.context import accepted_suggestion, render_history, render_recent_tasks
from app.graph.nodes._task_token import TASK_TOKEN, render_task_token
from app.graph.state import ActiveTask, OutboundDraft, RecentTaskEntry, State

log = structlog.get_logger(__name__)

_ACCEPTANCE_BODY = f"{TASK_TOKEN} is yours — say done when you finish."


async def _accept_suggestion(
    state: State, entry: RecentTaskEntry, now: datetime
) -> dict[str, Any]:
    """Mark the accepted suggestion In Progress and make it the active task."""
    from app.graph.nodes.selection import _extract_number, _extract_select
    from app.tools import notion

    page_id = entry["page_id"]
    try:
        await notion.update_status(page_id, "In Progress")
    except Exception:
        log.exception("chat_node.mark_in_progress_failed", page_id=page_id)

    props: dict[str, Any] = {}
    try:
        page = await notion.get_page(page_id)
        raw_props = page.get("properties") if isinstance(page, dict) else None
        if isinstance(raw_props, dict):
            props = raw_props
    except Exception:
        log.warning("chat_node.get_page_failed", page_id=page_id)

    # Defaults match selection_node's for a page missing a property.
    active_task = ActiveTask(
        page_id=page_id,
        title=entry["title"],
        status="In Progress",
        selected_at=now.isoformat(),
        work_type=_extract_select(props, "Work Type"),
        urgency=_extract_number(props, "Urgency", 50),
        time_estimate=_extract_number(props, "Time Estimate (min)", 30),
        energy_required=_extract_select(props, "Energy Required"),
        rejection_count=_extract_number(props, "Rejection Count", 0),
    )
    draft: OutboundDraft = {
        "recipient": state.get("peer", ""),
        # Rendered here from the stored title (send_node would do the same);
        # the draft still carries notion_page_title for the naming invariant.
        "body": render_task_token(_ACCEPTANCE_BODY, title=entry["title"]),
        "notion_page_id": page_id,
        "notion_page_title": entry["title"],
    }
    log.info("chat_node.acceptance", page_id=page_id)
    return {
        "pending_outbound": [draft],
        "active_task": active_task,
        "conversation_state": "active",
    }

async def chat_node(state: State) -> dict[str, Any]:
    """CHAT handler: general conversation fallback using medium-tier LLM."""
    peer = state.get("peer", "")
    incoming = state.get("incoming", "")

    try:
        now = datetime.now(UTC)
        accepted = accepted_suggestion(state, now=now)
        if accepted is not None:
            return await _accept_suggestion(state, accepted, now)

        from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage

        from app.models import llm
        from app.prompts.loader import render_with_defaults

        messages_history: list[AnyMessage] = state.get("messages", [])
        conversation_context = render_history(messages_history)
        # The ledger is what lets "what task?" be answered after a reply that
        # did not repeat the title; the active task covers "is that mine now?".
        recent_tasks = render_recent_tasks(state.get("recent_tasks"), now=now)
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
