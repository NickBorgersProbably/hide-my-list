"""Regression: a named task asked "which task?" when the match call misfired (bug #NNNN).

With reasoning off, the cheap-tier confirmation of an unambiguous standalone
report sometimes came back unusable, and the node asked which task the user
meant about the task they had just named. Measured: the model's confidence on
these shapes is 1.0 on nearly every call; the misfire is copying the
candidate's 36-character page id back with characters dropped, which matches
no candidate and reads as a null match.

Two fixes, both pinned here. A report that is one title plus report filler
resolves without the model, so the three title-shaped reports complete even
with the model pinned to an answer the node would refuse. And the model never
sees a page id: candidates carry `t1`, `t2`, … and the answer maps back, so a
paraphrase that does need the model cannot fail on a mistyped UUID.

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


def _model(verdict: dict[str, Any]) -> AsyncMock:
    response = MagicMock()
    response.content = json.dumps(verdict)
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=response)
    return model


def _refused_model() -> MagicMock:
    """An answer the node refuses: the observed truncated page id."""
    return MagicMock(return_value=_model(
        {"matched_page_id": "9e8b0eaf-b35a-5d85-a9c1-013055c5", "confidence": 1.0}
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "titles", "target"),
    [
        ("finished washing the dishes", ("Wash the dishes", "Book the dentist"), 0),
        ("ok, cleaned out the garage", ("Clean out the garage", "Reply to the school email"), 0),
        ("ok, replied to the school email", ("Clean out the garage", "Reply to the school email"), 1),
    ],
)
async def test_a_named_report_completes_whatever_the_model_answers(
    message: str, titles: tuple[str, ...], target: int
) -> None:
    pages = [_page(f"<page_{index}>", title) for index, title in enumerate(titles)]
    target_id = f"<page_{target}>"
    update_status = AsyncMock()
    llm_factory = _refused_model()

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
async def test_a_paraphrase_resolves_through_an_alias_the_model_cannot_mistype() -> None:
    """The paraphrase still needs the model; the model never sees a page id."""
    fridge = "9e8b0eaf-b35a-5d85-a9c1-013055c5c21f"
    dentist = "0d4f5a1e-6c2b-4b8e-9a77-1f3c2e5d6b70"
    pages = [_page(fridge, "Deal with the spare refrigerator"), _page(dentist, "Book the dentist")]
    update_status = AsyncMock()
    model = _model({"matched_page_id": "t1", "confidence": 1.0})

    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", AsyncMock(return_value={"results": pages})),
        patch(
            "app.tools.rewards.maybe_reward",
            AsyncMock(return_value={"text": "Nice work!", "attachment_path": None}),
        ),
        patch.object(complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)),
        patch("app.models.llm", MagicMock(return_value=model)),
    ):
        result = await complete_module.complete_node(
            _state("got rid of that old fridge finally")
        )

    prompt = str(model.ainvoke.await_args.args[0][0].content)
    assert fridge not in prompt and dentist not in prompt
    assert update_status.await_args.kwargs == {"page_id": fridge, "new_status": "Completed"}
    assert result["pending_outbound"][0]["notion_page_id"] == fridge


@pytest.mark.asyncio
async def test_a_mistyped_page_id_is_still_refused() -> None:
    """The node never guesses from a partial id: the observed answer is no match."""
    fridge = "9e8b0eaf-b35a-5d85-a9c1-013055c5c21f"
    update_status = AsyncMock()

    with (
        patch("app.tools.notion.update_status", update_status),
        patch(
            "app.tools.notion.query_all",
            AsyncMock(return_value={"results": [_page(fridge, "Deal with the spare refrigerator")]}),
        ),
        patch("app.tools.rewards.maybe_reward", AsyncMock()),
        patch.object(complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)),
        patch("app.models.llm", _refused_model()),
    ):
        result = await complete_module.complete_node(
            _state("got rid of that old fridge finally")
        )

    update_status.assert_not_awaited()
    assert result["pending_outbound"][0]["notion_page_id"] is None


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
