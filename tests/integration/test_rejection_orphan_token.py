"""REJECT never sends a `{task}` token with no title behind it.

Two paths:

- Nothing on the hook (no active task, no fresh ledger suggestion): the node
  runs no prompt, reads and writes nothing in Notion, records nothing in the
  ledger, and replies with a fixed acknowledgement that names nothing.
- A task is active but the model names no listed, titled alternative: the
  sentence carrying the token is dropped (or the no-alternative reply is used),
  and the rejected page's count update still goes out with its usual payload.

All Notion and LLM calls are mocked. No network.
"""
from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from app.graph.state import State


def _model(content: str) -> Any:
    response = MagicMock()
    response.content = content
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=response)
    return model


def _state(**overrides: Any) -> State:
    state: dict[str, Any] = {
        "peer": "<test-reject-orphan>",
        "incoming": "never mind, I'll check later",
        "intent": "REJECT",
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
    state.update(overrides)
    return state  # type: ignore[return-value]


def _active(page_id: str = "<page_A>", title: str = "Placeholder active task") -> dict[str, Any]:
    return {
        "page_id": page_id,
        "title": title,
        "status": "In Progress",
        "selected_at": datetime.now(UTC).isoformat(),
        "rejection_count": 2,
    }


def _pending(page_id: str, title: str) -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Work Type": {"select": {"name": "Independent"}},
            "Time Estimate (min)": {"number": 15},
        },
    }


@pytest.mark.asyncio
async def test_nothing_active_replies_without_prompt_or_writes() -> None:
    from app.graph.nodes.rejection import NOTHING_ACTIVE_BODY, rejection_node

    query_pending = AsyncMock(return_value={"results": []})
    update_property = AsyncMock()
    llm = MagicMock()
    with (
        patch("app.tools.notion.query_pending", query_pending),
        patch("app.tools.notion.update_property", update_property),
        patch("app.models.llm", llm),
        capture_logs() as logs,
    ):
        result = await rejection_node(_state())

    assert result == {
        "pending_outbound": [
            {
                "recipient": "<test-reject-orphan>",
                "body": NOTHING_ACTIVE_BODY,
                "notion_page_id": None,
            }
        ]
    }
    assert "{task}" not in NOTHING_ACTIVE_BODY
    llm.assert_not_called()
    query_pending.assert_not_awaited()
    update_property.assert_not_awaited()
    assert "rejection_node.nothing_active" in {e["event"] for e in logs}


@pytest.mark.asyncio
async def test_stale_ledger_suggestion_counts_as_nothing_active() -> None:
    from app.graph.nodes.rejection import NOTHING_ACTIVE_BODY, rejection_node

    stale = [{
        "page_id": "<page_A>",
        "title": "Placeholder old suggestion",
        "kind": "task",
        "event": "suggested",
        "at": (datetime.now(UTC) - timedelta(days=3)).isoformat(),
    }]
    llm = MagicMock()
    with patch("app.models.llm", llm):
        result = await rejection_node(_state(recent_tasks=stale))

    assert result["pending_outbound"][0]["body"] == NOTHING_ACTIVE_BODY
    llm.assert_not_called()


@pytest.mark.asyncio
async def test_fresh_ledger_suggestion_still_runs_the_prompt() -> None:
    """The history-only path (no active task, a suggestion minutes ago) is kept."""
    from app.graph.nodes.rejection import rejection_node

    fresh = [{
        "page_id": "<page_A>",
        "title": "Placeholder recent suggestion",
        "kind": "task",
        "event": "suggested",
        "at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
    }]
    response = json.dumps({"alternative_task_id": "<page_B>", "user_message": "How about {task}?"})
    model = _model(response)
    with (
        patch("app.tools.notion.query_pending", AsyncMock(return_value={"results": [
            _pending("<page_B>", "Placeholder alternative"),
        ]})),
        patch("app.tools.notion.update_property", AsyncMock()),
        patch("app.models.llm", return_value=model),
    ):
        result = await rejection_node(_state(incoming="something else?", recent_tasks=fresh))

    model.ainvoke.assert_awaited_once()
    draft = result["pending_outbound"][0]
    assert draft["body"] == "How about Placeholder alternative?"
    assert draft["notion_page_title"] == "Placeholder alternative"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("alternative_id", "user_message", "expected_body"),
    [
        # No alternative at all, but the model wrote the token anyway.
        (None, "Got it. How about {task}?", "Got it."),
        # An id the model invented: nothing listed stands behind it.
        ("<page_unknown>", "Fair enough. Try {task}? It's quick.", "Fair enough. It's quick."),
        # Only the token sentence: the no-alternative reply goes out instead.
        (None, "How about {task}?", None),
    ],
)
async def test_active_task_without_valid_alternative_drops_the_token(
    alternative_id: str | None, user_message: str, expected_body: str | None
) -> None:
    from app.graph.nodes.rejection import NO_ALTERNATIVE_BODY, rejection_node
    from app.tools import notion

    response = json.dumps({"alternative_task_id": alternative_id, "user_message": user_message})
    update_property = AsyncMock()
    with (
        patch("app.tools.notion.query_pending", AsyncMock(return_value={"results": [
            _pending("<page_A>", "Placeholder active task"),
        ]})),
        patch("app.tools.notion.update_property", update_property),
        patch("app.models.llm", return_value=_model(response)),
        capture_logs() as logs,
    ):
        result = await rejection_node(_state(incoming="not that one", active_task=_active()))

    draft = result["pending_outbound"][0]
    assert "{task}" not in draft["body"]
    assert draft["body"] == (expected_body or NO_ALTERNATIVE_BODY)
    assert draft["notion_page_id"] is None
    assert "notion_page_title" not in draft
    assert "rejection_node.orphan_task_token" in {e["event"] for e in logs}

    # The rejected page's count still updates, with the real verb's shape.
    update_property.assert_awaited_once()
    call = update_property.await_args
    assert call is not None
    assert call.args == (
        "<page_A>",
        {"properties": {"Rejection Count": {"number": 3}}},
    )
    assert call.kwargs == {}
    params = list(inspect.signature(inspect.unwrap(notion.update_property)).parameters)
    assert len(call.args) <= len(params)
    # No alternative was offered, so none is recorded.
    assert [e["event"] for e in result["recent_tasks"]] == ["rejected"]
