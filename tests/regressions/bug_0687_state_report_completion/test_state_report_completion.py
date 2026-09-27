"""Regression: a state report was not matched as a completion (bug #687).

"<X> is scheduled!" against the only open task "Schedule <X>" returned no match
from the cheap-tier match call, which read the passive state as a future one.
The node then asked "Nice — which task was it: <one title>?".

These tests hold the deterministic side: the prompt the node sends carries the
state-report rule, and one context option is asked as "was it <title>?". The
model side is held by the eval fixtures named in README.md.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.graph.nodes import complete as complete_module
from app.graph.nodes._task_match import DedupCandidate
from app.graph.state import State

_TITLE = "Schedule the deep clean"


def _page(page_id: str, title: str) -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Status": {"select": {"name": "Pending"}},
            "Is Reminder": {"checkbox": False},
        },
    }


def _suggested(page_id: str, title: str, minutes_ago: int) -> dict[str, Any]:
    return {
        "page_id": page_id,
        "title": title,
        "kind": "task",
        "event": "suggested",
        "at": (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat(),
    }


def _state(incoming: str, recent_tasks: list[dict[str, Any]]) -> State:
    return {  # type: ignore[return-value]
        "peer": "<test-peer>",
        "incoming": incoming,
        "intent": "COMPLETE",
        "messages": [],
        "active_task": None,
        "streak": 0,
        "tasks_completed_today": 0,
        "user_prefs": {},
        "conversation_state": "idle",
        "recent_tasks": recent_tasks,
    }


def _no_match_factory() -> MagicMock:
    response = MagicMock()
    response.content = json.dumps({"matched_page_id": None, "confidence": 0.0})
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=response)
    return MagicMock(return_value=model)


async def _run(
    incoming: str, pages: list[dict[str, Any]], ledger: list[dict[str, Any]]
) -> tuple[dict[str, Any], AsyncMock, MagicMock]:
    update_status = AsyncMock()
    factory = _no_match_factory()
    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", AsyncMock(return_value={"results": pages})),
        patch("app.tools.rewards.maybe_reward", AsyncMock()),
        patch.object(complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)),
        patch("app.models.llm", factory),
    ):
        result = await complete_module.complete_node(_state(incoming, ledger))
    return result, update_status, factory


@pytest.mark.asyncio
async def test_the_match_prompt_reads_a_state_report_as_done() -> None:
    _, _, factory = await _run(
        "Deep clean is scheduled!",
        [_page("<page_A>", _TITLE)],
        [_suggested("<page_A>", _TITLE, 2)],
    )

    assert factory.call_args.args == ("cheap",)
    assert factory.call_args.kwargs == {"caller": "complete_title_match"}
    prompt = str(factory.return_value.ainvoke.await_args.args[0][0].content)
    flattened = " ".join(prompt.split())
    assert "A report of the resulting state counts as asserting it is done" in flattened
    assert '"X is scheduled"' in flattened
    # The guards stay beside it.
    assert "is about to start is NOT a match" in flattened
    assert "asserts nothing and is not a match" in flattened


@pytest.mark.asyncio
async def test_one_context_option_is_asked_as_was_it() -> None:
    """The observed ask was "Nice — which task was it: <one title>?"."""
    result, update_status, _ = await _run(
        "Deep clean is scheduled!",
        [_page("<page_A>", _TITLE)],
        [_suggested("<page_A>", _TITLE, 2)],
    )

    update_status.assert_not_awaited()
    body = result["pending_outbound"][0]["body"]
    assert body == f"Nice — was it {_TITLE}?"
    assert "which task" not in body.lower()
    assert result["pending_clarification"]["candidates"] == [
        {"page_id": "<page_A>", "title": _TITLE}
    ]


def test_the_re_ask_of_one_context_option_is_worded_differently() -> None:
    option = (DedupCandidate("<page_A>", _TITLE, 1.0),)
    first = complete_module._clarification_body(0, option, offerable=True, from_context=True)
    second = complete_module._clarification_body(1, option, offerable=True, from_context=True)
    assert second == f"Just checking — was it {_TITLE}?"
    assert first != second


@pytest.mark.asyncio
async def test_two_context_options_keep_the_choice_wording() -> None:
    result, _, _ = await _run(
        "Deep clean is scheduled!",
        [_page("<page_A>", _TITLE), _page("<page_B>", "Water the garden")],
        [
            _suggested("<page_A>", _TITLE, 2),
            _suggested("<page_B>", "Water the garden", 5),
        ],
    )

    assert result["pending_outbound"][0]["body"] == (
        f"Nice — which task was it: {_TITLE} or Water the garden?"
    )
