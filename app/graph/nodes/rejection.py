"""REJECT node: shame-safe rejection handling.

When the user rejects a suggested task, classifies the reason, updates
rejection count in Notion, and suggests an alternative.

Implements docs/ai-prompts/rejection.md behavior.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import structlog

from app.graph.context import (
    ledger_entry,
    record_task_event,
    record_turn_action,
    render_history,
    render_recent_tasks,
)
from app.graph.nodes._task_token import TASK_TOKEN, render_task_token
from app.graph.state import OutboundDraft, State

log = structlog.get_logger(__name__)

# The reply when nothing is on the hook: no active task and no fresh
# suggestion in the ledger. It names nothing, so it cannot name the wrong
# thing, and it leaves the next step with the user.
NOTHING_ACTIVE_BODY = (
    "No problem — nothing's on the hook right now. Want a suggestion when you're ready?"
)

# The reply when the model's message refers to an alternative the application
# cannot fill in (no listed alternative id, or no title for it).
NO_ALTERNATIVE_BODY = (
    "No problem — that helps me learn what works for you. "
    "Want me to find something different?"
)

# A "not that one" is about a suggestion made recently. An older ledger
# suggestion is not what the user is turning down.
_SUGGESTION_FRESHNESS = timedelta(hours=24)


def _has_fresh_suggestion(entries: Iterable[object] | None, *, now: datetime) -> bool:
    """Whether the recent-task ledger shows a titled suggestion from the last day.

    The checkpoint's `active_task` can be empty while the conversation still
    holds the suggestion the user is turning down (history and ledger carry
    it), and the rejection prompt reads that context. Only when both are
    empty is there nothing to reject.
    """
    for raw in entries or []:
        if not isinstance(raw, Mapping) or raw.get("event") != "suggested":
            continue
        title = raw.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        raw_at = raw.get("at")
        if isinstance(raw_at, str):
            try:
                at = datetime.fromisoformat(raw_at.replace("Z", "+00:00"))
            except ValueError:
                return True
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            if now - at > _SUGGESTION_FRESHNESS:
                continue
        return True
    return False


def _without_orphan_token(user_message: str) -> str:
    """Drop every sentence carrying a `{task}` token that no title can fill.

    Falls back to the no-alternative reply when nothing is left.
    """
    if TASK_TOKEN not in user_message:
        return user_message
    sentences = re.split(r"(?<=[.!?])\s+", user_message.strip())
    remainder = " ".join(s for s in sentences if TASK_TOKEN not in s).strip()
    return remainder or NO_ALTERNATIVE_BODY

async def rejection_node(state: State) -> dict[str, Any]:
    """REJECT handler: classify rejection and suggest alternative."""
    peer = state.get("peer", "")

    if not state.get("active_task") and not _has_fresh_suggestion(
        state.get("recent_tasks"), now=datetime.now(UTC)
    ):
        # Nothing to turn down: no prompt, no Notion read or write, no ledger
        # event. Running the prompt here invites a `{task}` with no title.
        log.info("rejection_node.nothing_active")
        nothing: OutboundDraft = {
            "recipient": peer,
            "body": NOTHING_ACTIVE_BODY,
            "notion_page_id": None,
        }
        return {"pending_outbound": [nothing]}

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.models import llm
        from app.prompts.loader import render_with_defaults
        from app.tools import notion

        incoming = state.get("incoming", "")
        active_task = state.get("active_task")
        available_minutes = state.get("available_minutes") or 30
        mood = state.get("mood") or "neutral"

        stored_title = (active_task.get("title") or "").strip() if active_task else ""
        task_title = stored_title or "the suggested task"
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
                "conversation_history": render_history(state.get("messages")),
                "recent_tasks": render_recent_tasks(
                    state.get("recent_tasks"), now=datetime.now(UTC)
                ),
            },
            defaults={
                "task_title": "the suggested task",
                "rejection_reason": "",
                "remaining_tasks_json": "[]",
                "available_minutes": 30,
                "mood": "neutral",
                "conversation_history": "No prior context.",
                "recent_tasks": "None yet.",
            },
        )

        model = llm("medium", caller="rejection")
        messages = [
            SystemMessage(content=prompt_text),
            HumanMessage(content=f"The user said: {incoming!r}"),
        ]

        response = await model.ainvoke(messages)
        response_text = str(response.content).strip()

        user_message, alternative_id = _parse_rejection_response(response_text)
        alternative_title = _alternative_task_title(alternative_id, remaining)
        if alternative_title:
            user_message = render_task_token(user_message, title=alternative_title)
        elif TASK_TOKEN in user_message:
            # A token with no listed, titled alternative behind it would reach
            # the user verbatim, and the id names nothing the user was shown.
            log.info(
                "rejection_node.orphan_task_token",
                had_alternative_id=bool(alternative_id),
            )
            user_message = _without_orphan_token(user_message)
            alternative_id = None

        # Update rejection count in Notion
        turn_actions = list(state.get("turn_actions") or [])
        if rejected_page_id:
            try:
                await notion.update_property(
                    rejected_page_id,
                    {
                        "properties": {
                            "Rejection Count": {
                                "number": active_task.get("rejection_count", 0) + 1
                                if active_task
                                else 1
                            }
                        }
                    },
                )
                turn_actions = record_turn_action(
                    turn_actions, action="notion.update_property", page_id=rejected_page_id
                )
            except Exception:
                log.exception("rejection_node.notion_update_failed", page_id=rejected_page_id)

        draft: OutboundDraft = {
            "recipient": peer,
            "body": user_message,
            "notion_page_id": alternative_id,
        }
        if alternative_title:
            draft["notion_page_title"] = alternative_title

        # The ledger remembers both halves of the exchange: the declined page as
        # `rejected`, then the offered alternative as `suggested`. Recorded in
        # that order at the same instant, so the alternative is the ledger's
        # newest entry and a bare "done" next turn anchors to it. Only a listed
        # alternative with a real title is recorded — an id the model invented
        # names nothing the user was shown.
        now = datetime.now(UTC)
        recent_tasks = list(state.get("recent_tasks") or [])
        if rejected_page_id:
            known = ledger_entry(recent_tasks, rejected_page_id)
            recent_tasks = record_task_event(
                recent_tasks,
                page_id=rejected_page_id,
                title=stored_title,
                kind=known["kind"] if known else "task",
                event="rejected",
                now=now,
            )
        if alternative_id and alternative_title and alternative_title.strip():
            recent_tasks = record_task_event(
                recent_tasks,
                page_id=alternative_id,
                title=alternative_title,
                kind="task",
                event="suggested",
                now=now,
            )
            turn_actions = record_turn_action(
                turn_actions, action="suggest", page_id=alternative_id
            )

        log.info(
            "rejection_node.alternative",
            alternative_id=alternative_id,
            alternative_recorded=bool(alternative_id and alternative_title),
        )
        return {
            "pending_outbound": [draft],
            "active_task": None,
            "conversation_state": "selection",
            "recent_tasks": recent_tasks,
            "turn_actions": turn_actions,
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
            loaded = json.loads(json_match.group())
            if not isinstance(loaded, dict):
                return response_text[:300], None
            data = cast(dict[str, Any], loaded)
            user_message = data.get("user_message", response_text[:300])
            alternative_id = data.get("alternative_task_id")
            return (
                user_message if isinstance(user_message, str) else response_text[:300],
                alternative_id if isinstance(alternative_id, str) else None,
            )
        except json.JSONDecodeError:
            pass
    return response_text[:300] if response_text else "No problem. Want something different?", None


def _alternative_task_title(
    alternative_id: str | None,
    remaining: list[dict[str, Any]],
) -> str | None:
    """Return the title the model selected as the alternative, if it exists."""
    if not alternative_id:
        return None

    title_by_id = {
        task.get("id"): task.get("title")
        for task in remaining
        if isinstance(task.get("id"), str) and isinstance(task.get("title"), str)
    }
    return title_by_id.get(alternative_id)


def _extract_title(props: dict[str, Any]) -> str:
    title_prop = props.get("Title", {})
    if not isinstance(title_prop, dict):
        return ""
    items = title_prop.get("title", [])
    if not isinstance(items, list):
        return ""
    parts: list[str] = []
    for item in items:
        if isinstance(item, dict):
            plain_text = item.get("plain_text", "")
            if isinstance(plain_text, str):
                parts.append(plain_text)
    return "".join(parts)


def _extract_select(props: dict[str, Any], key: str) -> str:
    prop = props.get(key, {})
    if not isinstance(prop, dict):
        return ""
    sel = prop.get("select") or {}
    if not isinstance(sel, dict):
        return ""
    name = sel.get("name", "")
    return name if isinstance(name, str) else ""


def _extract_number(props: dict[str, Any], key: str, default: int = 0) -> int:
    prop = props.get(key, {})
    if not isinstance(prop, dict):
        return default
    num = prop.get("number")
    return int(num) if num is not None else default
