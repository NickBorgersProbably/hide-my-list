"""Regression: selection never suggests a task it cannot name.

The model returned an id outside the scored list; the node marked that page In
Progress and built an ActiveTask with an empty title, and the user was offered
a task with no name. Placeholder titles and ids only.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

_RETRY = "Couldn't land on one just now — ask me again in a sec?"


def _page(page_id: str, title: str) -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Status": {"select": {"name": "Pending"}},
            "Work Type": {"select": {"name": "Focus"}},
            "Energy Required": {"select": {"name": "Medium"}},
            "Urgency": {"number": 50},
            "Time Estimate (min)": {"number": 20},
            "Rejection Count": {"number": 0},
        },
    }


def _model(payload: dict[str, Any]) -> Any:
    response = MagicMock()
    response.content = json.dumps(payload)
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=response)
    return model


def _state() -> dict[str, Any]:
    return {
        "peer": "<recipient>",
        "incoming": "anything I can knock out right now?",
        "intent": "GET_TASK",
        "messages": [],
        "active_task": None,
        "streak": 0,
        "tasks_completed_today": 0,
        "user_prefs": {},
        "mood": None,
        "available_minutes": None,
        "conversation_state": "idle",
        "pending_outbound": [],
        "recent_tasks": [],
    }


async def _run(
    pages: list[dict[str, Any]], payload: dict[str, Any]
) -> tuple[dict[str, Any], AsyncMock, list[Any]]:
    from app.graph.nodes.selection import selection_node

    update_status = AsyncMock()
    with (
        patch("app.tools.notion.query_pending", AsyncMock(return_value={"results": pages})),
        patch("app.tools.notion.update_status", update_status),
        patch("app.models.llm", return_value=_model(payload)),
        capture_logs() as logs,
    ):
        result = await selection_node(_state())  # type: ignore[arg-type]
    return result, update_status, logs


def _assert_no_selection(result: dict[str, Any], update_status: AsyncMock) -> None:
    from app.graph.nodes._task_token import render_task_token

    update_status.assert_not_awaited()
    assert result["active_task"] is None
    assert result["conversation_state"] == "selection"
    assert result["recent_tasks"] == []
    draft = result["pending_outbound"][0]
    assert draft["notion_page_id"] is None
    assert "notion_page_title" not in draft
    delivered = render_task_token(draft["body"], title=draft.get("notion_page_title"))
    assert "{task}" not in delivered
    assert "focus task" not in delivered


@pytest.mark.asyncio
async def test_unknown_selected_id_is_not_suggested() -> None:
    result, update_status, logs = await _run(
        [_page("<page_A>", "Placeholder listed task")],
        {
            "selected_task_id": "<page_not_listed>",
            "score": 0.9,
            "reasoning": "fits",
            "user_message": "Perfect timing - how about this focus task?",
        },
    )
    _assert_no_selection(result, update_status)
    events = [e for e in logs if e.get("event") == "selection_node.unknown_page_id"]
    assert len(events) == 1
    assert events[0]["has_selection"] is True
    assert events[0]["in_candidates"] is False
    assert events[0]["blank_title"] is False
    assert events[0]["candidate_count"] == 1
    # The id is model-supplied free text: never logged.
    assert "<page_not_listed>" not in repr(events[0])
    assert result["pending_outbound"][0]["body"] == _RETRY


@pytest.mark.asyncio
async def test_listed_page_with_blank_title_is_not_suggested() -> None:
    result, update_status, logs = await _run(
        [_page("<page_blank>", "")],
        {
            "selected_task_id": "<page_blank>",
            "score": 0.9,
            "reasoning": "fits",
            "user_message": "How about {task}?",
        },
    )
    _assert_no_selection(result, update_status)
    events = [e for e in logs if e.get("event") == "selection_node.unknown_page_id"]
    assert len(events) == 1
    assert events[0]["in_candidates"] is True
    assert events[0]["blank_title"] is True
    assert "<page_blank>" not in repr(events[0])
    assert result["pending_outbound"][0]["body"] == _RETRY


@pytest.mark.asyncio
async def test_token_without_selection_gets_the_no_match_reply() -> None:
    result, update_status, _ = await _run(
        [_page("<page_A>", "Placeholder listed task")],
        {"selected_task_id": None, "score": 0.0, "reasoning": "", "user_message": "How about {task}?"},
    )
    _assert_no_selection(result, update_status)
    assert result["pending_outbound"][0]["body"] == (
        "Nothing quite fits right now. Want to add something quick?"
    )
