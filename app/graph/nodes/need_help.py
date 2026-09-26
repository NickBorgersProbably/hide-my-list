"""NEED_HELP node: breakdown assistance.

When the user needs help starting or continuing a task, provides specific,
actionable guidance matched to their confidence level.

The task to help with is the checkpointed `active_task`. With none, it is the
newest task the recent-task ledger shows as just added or just suggested — a
"how do I start?" right after adding a task is about that task, not a request
to pick a new one. Only with neither does the node ask to find a task first.

Implements docs/ai-prompts/breakdown.md behavior.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.graph.context import render_history, render_recent_tasks
from app.graph.state import OutboundDraft, State

log = structlog.get_logger(__name__)

# Ledger events that make a task the one "how do I start?" is about. A task
# that was completed, rejected, or only reminded is not one the user is
# about to start.
_HELPABLE_EVENTS = frozenset({"added", "suggested"})

# "how do I start?" right after adding or suggesting a task is about that task.
# An entry outside this window is too stale to be the current referent.
_LEDGER_HELP_FRESHNESS = timedelta(hours=24)


def _ledger_task(
    entries: Iterable[object] | None, *, now: datetime
) -> tuple[str, str] | None:
    """Return `(page_id, title)` of the newest fresh added/suggested ledger entry.

    The ledger is stored newest first with one entry per page, so the first
    entry whose latest event is added or suggested is the task the user most
    recently took on. An entry with no title is skipped: help that cannot name
    its task is the failure this fallback exists to avoid. An entry outside
    `_LEDGER_HELP_FRESHNESS` is skipped: a "how do I start?" is about a task
    the user just added, not one from days ago.
    """
    for raw in entries or []:
        if not isinstance(raw, Mapping):
            continue
        page_id = raw.get("page_id")
        title = raw.get("title")
        if raw.get("event") not in _HELPABLE_EVENTS:
            continue
        if not isinstance(page_id, str) or not page_id:
            continue
        if not isinstance(title, str) or not title.strip():
            continue
        raw_at = raw.get("at")
        if isinstance(raw_at, str):
            try:
                at = datetime.fromisoformat(raw_at.replace("Z", "+00:00"))
                if at.tzinfo is None:
                    at = at.replace(tzinfo=UTC)
                else:
                    at = at.astimezone(UTC)
                if now - at > _LEDGER_HELP_FRESHNESS:
                    continue
            except ValueError:
                pass
        return page_id, title.strip()
    return None


async def need_help_node(state: State) -> dict[str, Any]:
    """NEED_HELP handler: provide actionable breakdown guidance."""
    peer = state.get("peer", "")

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.models import llm
        from app.prompts.loader import render_with_defaults

        incoming = state.get("incoming", "")
        active_task = state.get("active_task")

        if active_task:
            real_title: str | None = active_task.get("title")
            page_id = active_task.get("page_id", "")
            # inline_steps may be stored in active_task or fetched from Notion
            inline_steps = active_task.get("inline_steps", "No steps recorded yet.")
        else:
            now = datetime.now(UTC)
            ledger_task = _ledger_task(state.get("recent_tasks"), now=now)
            if ledger_task is None:
                no_task_draft: OutboundDraft = {
                    "recipient": peer,
                    "body": "Let's get you a task first! How much time do you have?",
                    "notion_page_id": None,
                }
                return {"pending_outbound": [no_task_draft]}
            page_id, real_title = ledger_task
            inline_steps = "No steps recorded yet."
            try:
                from app.tools import notion as _notion_mod
                page = await _notion_mod.get_page(page_id=page_id)
                props = page.get("properties", {}) if isinstance(page, dict) else {}
                il_prop = (
                    props.get("Inline Steps", {}) if isinstance(props, dict) else {}
                )
                items = il_prop.get("rich_text", []) if isinstance(il_prop, dict) else []
                fetched = " ".join(
                    str(item.get("plain_text", ""))
                    for item in items
                    if isinstance(item, dict)
                ).strip()
                if fetched:
                    inline_steps = fetched
            except Exception:
                log.info("need_help_node.inline_steps_fetch_failed", has_page_id=bool(page_id))
            log.info("need_help_node.ledger_task", has_page_id=bool(page_id))

        task_title = (real_title or "").strip() or "your task"

        prompt_text = render_with_defaults(
            "need_help.md.j2",
            {
                "task_title": task_title,
                "inline_steps": inline_steps,
                "user_message": incoming,
                "conversation_history": render_history(state.get("messages")),
                "recent_tasks": render_recent_tasks(
                    state.get("recent_tasks"), now=datetime.now(UTC)
                ),
            },
            defaults={
                "task_title": "your task",
                "inline_steps": "No steps available.",
                "user_message": "",
                "conversation_history": "No prior context.",
                "recent_tasks": "None yet.",
            },
        )

        model = llm("medium", caller="need_help")
        messages = [
            SystemMessage(content=prompt_text),
            HumanMessage(content=incoming),
        ]

        response = await model.ainvoke(messages)
        response_text = str(response.content).strip()

        user_message = _parse_need_help_response(response_text)

        draft: OutboundDraft = {
            "recipient": peer,
            "body": user_message,
            "notion_page_id": page_id,
        }
        if real_title:
            draft["notion_page_title"] = real_title

        log.info("need_help_node.response", has_peer=bool(peer))
        return {"pending_outbound": [draft]}

    except Exception:
        log.exception("need_help_node.error", has_peer=bool(peer))
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
