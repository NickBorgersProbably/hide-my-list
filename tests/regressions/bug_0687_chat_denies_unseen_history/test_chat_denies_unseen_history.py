"""Regression: chat denied a nudge the 7-day ledger no longer showed (bug #687).

The user asked which deadline the assistant had nudged them about. The nudges
were older than the ledger window, Recent Tasks was empty, and the model said
it had never nudged them. The same session offered "want to see your other
tasks?", which no module can honor.

These tests hold the prompt side: with an empty ledger, the system prompt the
node actually sends carries the no-record rule and the never-list guideline.
The model side is held by the chat eval fixtures named in README.md.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.graph.nodes.chat import chat_node
from app.graph.state import State


def _state(incoming: str) -> State:
    return {  # type: ignore[return-value]
        "peer": "<test-peer>",
        "incoming": incoming,
        "intent": "CHAT",
        "messages": [],
        "active_task": None,
        "conversation_state": "idle",
        "recent_tasks": [],
    }


def _model(reply: str) -> AsyncMock:
    response = MagicMock()
    response.content = reply
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=response)
    return model


async def _system_prompt_for(incoming: str) -> tuple[str, MagicMock, dict[str, Any]]:
    model = _model("I don't have a record of a recent one. What was it about?")
    factory = MagicMock(return_value=model)
    with patch("app.models.llm", factory):
        result = await chat_node(_state(incoming))
    prompt = str(model.ainvoke.await_args.args[0][0].content)
    return prompt, factory, result


@pytest.mark.asyncio
async def test_an_empty_ledger_is_not_read_as_never() -> None:
    prompt, factory, result = await _system_prompt_for(
        "What deadline were you nudging me for?"
    )

    assert factory.call_args.args == ("medium",)
    assert factory.call_args.kwargs == {"caller": "chat"}
    assert "None yet." in prompt  # the ledger really is empty in this turn
    section = prompt.split("### No record is not never", 1)[1].split("\n### ", 1)[0]
    assert "no record of a recent one" in section
    assert "Never say it did not happen" in section
    assert result["pending_outbound"][0]["body"].startswith("I don't have a record")


@pytest.mark.asyncio
async def test_chat_never_offers_to_show_the_list() -> None:
    prompt, _, _ = await _system_prompt_for("cool, what else is there?")

    guidelines = prompt.split("### Response Guidelines", 1)[1].split("\n### ", 1)[0]
    flattened = " ".join(guidelines.split())
    assert "Never offer to show, list, or enumerate the user's tasks" in flattened
    assert "want to see your other tasks?" in flattened
    assert "one task suggestion" in flattened
