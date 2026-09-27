"""Regression: REJECT never delivers a literal `{task}` token.

The reported failure: a REJECT-classified "never mind" with nothing active ran
the rejection prompt, the model wrote "How about {task}?" with no alternative,
and the token reached the user. These tests pass the node's draft through the
real `send_node` so the assertion is on the delivered text.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.graph.state import State

_TOKEN_REPLY = json.dumps({
    "rejection_category": "general",
    "alternative_task_id": None,
    "user_message": "No problem — here's something different: {task}?",
})


def _model(content: str) -> Any:
    response = MagicMock()
    response.content = content
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=response)
    return model


def _state(**overrides: Any) -> State:
    state: dict[str, Any] = {
        "peer": "<recipient>",
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


async def _deliver(result: dict[str, Any]) -> list[str]:
    from app.graph.nodes.send import send_node

    send = AsyncMock(return_value={"timestamp": 1})
    with patch("app.tools.signal_client.send_message", send):
        await send_node(_state(pending_outbound=result["pending_outbound"]))
    return [call.kwargs["message"] for call in send.await_args_list]


@pytest.mark.asyncio
async def test_never_mind_with_nothing_active_names_nothing() -> None:
    from app.graph.nodes.rejection import NOTHING_ACTIVE_BODY, rejection_node

    model = _model(_TOKEN_REPLY)
    update_property = AsyncMock()
    with (
        patch("app.tools.notion.query_pending", AsyncMock(return_value={"results": []})),
        patch("app.tools.notion.update_property", update_property),
        patch("app.models.llm", return_value=model),
    ):
        result = await rejection_node(_state())

    model.ainvoke.assert_not_awaited()
    update_property.assert_not_awaited()
    assert "recent_tasks" not in result
    assert await _deliver(result) == [NOTHING_ACTIVE_BODY]


@pytest.mark.asyncio
async def test_active_task_with_a_token_and_no_alternative_delivers_no_token() -> None:
    from app.graph.nodes.rejection import NO_ALTERNATIVE_BODY, rejection_node

    active = {"page_id": "<page_A>", "title": "Placeholder task", "rejection_count": 0}
    with (
        patch("app.tools.notion.query_pending", AsyncMock(return_value={"results": []})),
        patch("app.tools.notion.update_property", AsyncMock()),
        patch("app.models.llm", return_value=_model(_TOKEN_REPLY)),
    ):
        result = await rejection_node(_state(incoming="not that one", active_task=active))

    delivered = await _deliver(result)
    assert delivered == [NO_ALTERNATIVE_BODY]
    assert all("{task}" not in body for body in delivered)
