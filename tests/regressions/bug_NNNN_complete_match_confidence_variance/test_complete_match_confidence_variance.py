"""Regression: a named task asked "which task?" on a low model score (bug #NNNN).

With reasoning off, the cheap-tier confirmation of an unambiguous standalone
report ("finished washing the dishes" against "Wash the dishes") came back
under the 0.90 threshold on some runs, and the node asked which task the user
meant about the task they had just named. Each test pins the model to the
sub-threshold answer it gave on those runs; the report must still complete
the named task, because a report that is one title plus report filler no
longer depends on the model at all.

The guard the model path exists for is held here too: a report that also
names what comes next still goes to the model and does not complete.
"""
from __future__ import annotations

import inspect
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.graph.nodes import complete as complete_module
from app.graph.state import State
from app.tools import notion


def _page(page_id: str, title: str) -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Status": {"select": {"name": "Pending"}},
            "Is Reminder": {"checkbox": False},
        },
    }


def _state(incoming: str) -> State:
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
        "recent_tasks": [],
    }


def _sub_threshold_model(page_id: str) -> MagicMock:
    response = MagicMock()
    response.content = json.dumps({"matched_page_id": page_id, "confidence": 0.85})
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=response)
    return MagicMock(return_value=model)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "titles", "target"),
    [
        ("finished washing the dishes", ("Wash the dishes", "Book the dentist"), 0),
        ("ok, cleaned out the garage", ("Clean out the garage", "Reply to the school email"), 0),
        ("ok, replied to the school email", ("Clean out the garage", "Reply to the school email"), 1),
    ],
)
async def test_a_named_report_completes_whatever_the_model_scores(
    message: str, titles: tuple[str, ...], target: int
) -> None:
    pages = [_page(f"<page_{index}>", title) for index, title in enumerate(titles)]
    target_id = f"<page_{target}>"
    update_status = AsyncMock()
    llm_factory = _sub_threshold_model(target_id)

    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", AsyncMock(return_value={"results": pages})),
        patch(
            "app.tools.rewards.maybe_reward",
            AsyncMock(return_value={"text": "Nice work!", "attachment_path": None}),
        ),
        patch.object(complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)),
        patch("app.models.llm", llm_factory),
    ):
        result = await complete_module.complete_node(_state(message))

    llm_factory.assert_not_called()
    update_status.assert_awaited_once()
    kwargs = update_status.await_args.kwargs
    assert set(kwargs) <= set(inspect.signature(notion.update_status).parameters)
    assert kwargs == {"page_id": target_id, "new_status": "Completed"}
    assert result["pending_outbound"][0]["notion_page_id"] == target_id
    assert result.get("pending_clarification") is None


@pytest.mark.asyncio
async def test_a_report_that_names_the_next_task_still_asks_the_model() -> None:
    update_status = AsyncMock()
    response = MagicMock()
    response.content = json.dumps({"matched_page_id": None, "confidence": 0.0})
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=response)

    with (
        patch("app.tools.notion.update_status", update_status),
        patch(
            "app.tools.notion.query_all",
            AsyncMock(return_value={"results": [_page("<page_0>", "Call mom")]}),
        ),
        patch("app.tools.rewards.maybe_reward", AsyncMock()),
        patch.object(complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)),
        patch("app.models.llm", MagicMock(return_value=model)),
    ):
        result = await complete_module.complete_node(_state("done, now I need to call mom"))

    model.ainvoke.assert_awaited_once()
    update_status.assert_not_awaited()
    assert result["pending_outbound"][0]["notion_page_id"] is None
