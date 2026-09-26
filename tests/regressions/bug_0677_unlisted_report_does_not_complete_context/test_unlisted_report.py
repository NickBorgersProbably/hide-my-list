"""Regression: a report of something unlisted must not complete the context task (#677).

"I also paid the gas bill!" names a finished task that is on none of the
candidates. The model says so (`names_unlisted_task: true`), and the node asks
rather than completing whatever the conversation last touched.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.graph.nodes import complete as complete_module
from app.graph.state import State

_WITH_OPTION = "Nice one — want me to log that as done, or did you mean {task}?"
_NO_OPTION = "Nice one — want me to log that as done?"


def _notion_page(page_id: str, title: str) -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Status": {"select": {"name": "Pending"}},
            "Is Reminder": {"checkbox": False},
        },
    }


def _model(payload: dict[str, Any]) -> AsyncMock:
    response = MagicMock()
    response.content = json.dumps(payload)
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=response)
    return model


def _state(incoming: str, *, active_task: dict[str, Any] | None = None) -> State:
    return {  # type: ignore[return-value]
        "peer": "<test-peer>",
        "incoming": incoming,
        "intent": "COMPLETE",
        "messages": [],
        "active_task": active_task,
        "streak": 1,
        "tasks_completed_today": 0,
        "user_prefs": {},
        "mood": None,
        "available_minutes": None,
        "conversation_state": "idle",
        "pending_outbound": [],
        "pending_clarification": None,
    }


def _live_active(page_id: str, title: str) -> dict[str, Any]:
    return {
        "page_id": page_id,
        "title": title,
        "selected_at": datetime.now(UTC).isoformat(),
        "work_type": "Physical",
        "energy_required": "Low",
    }


async def _run(
    incoming: str,
    *,
    pages: list[dict[str, Any]],
    verdict: dict[str, Any],
    active_task: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], AsyncMock, AsyncMock]:
    update_status = AsyncMock()
    reward_mock = AsyncMock(return_value={"text": "Nice work!", "attachment_path": None})
    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", AsyncMock(return_value={"results": pages})),
        patch("app.tools.rewards.maybe_reward", reward_mock),
        patch.object(
            complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)
        ),
        patch("app.models.llm", return_value=_model(verdict)),
    ):
        result = await complete_module.complete_node(_state(incoming, active_task=active_task))
    return result, update_status, reward_mock


_DECOYS = [
    _notion_page("<page_A>", "Fold the laundry"),
    _notion_page("<page_B>", "Book the dentist appointment"),
]
_UNLISTED = {"matched_page_id": None, "confidence": 0.0, "names_unlisted_task": True}


@pytest.mark.asyncio
async def test_an_unlisted_report_does_not_complete_the_active_task() -> None:
    result, update_status, reward_mock = await _run(
        "I also paid the gas bill!",
        pages=_DECOYS,
        verdict=_UNLISTED,
        active_task=_live_active("<page_A>", "Fold the laundry"),
    )

    update_status.assert_not_awaited()
    reward_mock.assert_not_awaited()
    draft = result["pending_outbound"][0]
    assert draft["body"] == _WITH_OPTION
    assert draft["notion_page_title"] == "Fold the laundry"
    assert draft["notion_page_id"] is None
    clarification = result["pending_clarification"]
    assert clarification["kind"] == "complete_target"
    assert clarification["candidates"][0]["page_id"] == "<page_A>"
    assert "recent_tasks" not in result


@pytest.mark.asyncio
async def test_an_unlisted_report_over_a_scored_shortlist_also_asks() -> None:
    """The flag wins whether or not the message overlapped a title."""
    result, update_status, reward_mock = await _run(
        "paid the dentist bill",
        pages=_DECOYS,
        verdict=_UNLISTED,
        active_task=_live_active("<page_A>", "Fold the laundry"),
    )

    update_status.assert_not_awaited()
    reward_mock.assert_not_awaited()
    assert result["pending_outbound"][0]["body"] == _WITH_OPTION
    assert result["pending_clarification"] is not None


@pytest.mark.asyncio
async def test_an_unlisted_report_with_no_context_asks_to_add_it() -> None:
    result, update_status, reward_mock = await _run(
        "I also paid the gas bill!", pages=_DECOYS, verdict=_UNLISTED
    )

    update_status.assert_not_awaited()
    reward_mock.assert_not_awaited()
    assert result["pending_outbound"][0]["body"] == _NO_OPTION
    assert result["pending_clarification"]["candidates"] == []


@pytest.mark.asyncio
async def test_without_the_flag_a_widened_null_match_still_lets_context_resolve() -> None:
    """The #664 rule is unchanged when the model makes no unlisted claim."""
    result, update_status, _ = await _run(
        "done :) feeling good",
        pages=[_notion_page("<page_B>", "Book the dentist appointment")],
        verdict={"matched_page_id": None, "confidence": 0.0, "names_unlisted_task": False},
        active_task=_live_active("<page_A>", "Fold the laundry"),
    )

    update_status.assert_awaited_once()
    assert update_status.await_args.kwargs["page_id"] == "<page_A>"
    assert result["pending_clarification"] is None


@pytest.mark.asyncio
async def test_a_confident_match_ignores_a_contradictory_flag() -> None:
    """A named candidate is a match; the flag only speaks when nothing matched."""
    result, update_status, _ = await _run(
        "finished booking the dentist appointment",
        pages=_DECOYS,
        verdict={"matched_page_id": "<page_B>", "confidence": 0.95, "names_unlisted_task": True},
    )

    update_status.assert_awaited_once()
    assert update_status.await_args.kwargs["page_id"] == "<page_B>"
    assert result["pending_clarification"] is None


@pytest.mark.asyncio
async def test_an_unlisted_report_with_an_empty_list_asks_to_add_it() -> None:
    """No open tasks: the model is still asked, and a concrete report gets the add question."""
    model = _model(_UNLISTED)
    update_status = AsyncMock()
    reward_mock = AsyncMock(return_value={"text": "Nice work!", "attachment_path": None})
    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", AsyncMock(return_value={"results": []})),
        patch("app.tools.rewards.maybe_reward", reward_mock),
        patch.object(
            complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)
        ),
        patch("app.models.llm", return_value=model),
    ):
        result = await complete_module.complete_node(_state("I also paid the gas bill!"))

    model.ainvoke.assert_awaited_once()
    prompt = model.ainvoke.await_args.args[0][0].content
    assert "names_unlisted_task" in prompt
    assert "Candidates: []" in prompt
    update_status.assert_not_awaited()
    reward_mock.assert_not_awaited()
    assert result["pending_outbound"][0]["body"] == _NO_OPTION
    assert result["pending_clarification"]["candidates"] == []


@pytest.mark.asyncio
async def test_an_empty_list_without_the_flag_keeps_the_generic_question() -> None:
    result, update_status, _ = await _run(
        "I also paid the gas bill!",
        pages=[],
        verdict={"matched_page_id": None, "confidence": 0.0, "names_unlisted_task": False},
    )

    update_status.assert_not_awaited()
    assert result["pending_outbound"][0]["body"] != _NO_OPTION
    assert result["pending_clarification"] is not None


@pytest.mark.asyncio
async def test_an_empty_list_with_a_one_word_residue_skips_the_model() -> None:
    model = _model(_UNLISTED)
    with (
        patch("app.tools.notion.update_status", AsyncMock()),
        patch("app.tools.notion.query_all", AsyncMock(return_value={"results": []})),
        patch("app.tools.rewards.maybe_reward", AsyncMock()),
        patch.object(
            complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)
        ),
        patch("app.models.llm", return_value=model),
    ):
        result = await complete_module.complete_node(_state("finished laundry"))

    model.ainvoke.assert_not_awaited()
    assert result["pending_outbound"][0]["body"] != _NO_OPTION
