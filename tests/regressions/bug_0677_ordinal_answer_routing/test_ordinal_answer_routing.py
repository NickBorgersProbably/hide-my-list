"""Regression: a positional answer to an open clarification routes to COMPLETE.

See README.md. Placeholder peers, page ids, and titles only.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest

from app.graph.routing import _INTENT_SYSTEM_PROMPT
from app.graph.state import State


def _state(incoming: str) -> State:
    return {  # type: ignore[typeddict-item]
        "peer": "<test-peer>",
        "incoming": incoming,
        "intent": "CHAT",
        "messages": [],
        "active_task": None,
        "conversation_state": "idle",
        "pending_outbound": [],
        "pending_clarification": {
            "kind": "complete_target",
            "asked_at": datetime.now(UTC).isoformat(),
            "attempts": 1,
            "candidates": [
                {"page_id": "<page-id-1>", "title": "Test task one"},
                {"page_id": "<page-id-2>", "title": "Test task two"},
            ],
        },
    }


class _AddTaskLLM:
    """The model's wrong answer from the bug: every message is ADD_TASK."""

    calls = 0

    def __call__(self, _tier: str, **_kwargs: Any) -> Any:
        outer = self

        class _Resp:
            content = "ADD_TASK"

        class _Model:
            async def ainvoke(self, _msgs: list[Any]) -> Any:
                outer.calls += 1
                return _Resp()

        return _Model()


@pytest.mark.asyncio
async def test_the_first_one_keeps_the_clarification_even_if_the_model_says_add_task() -> None:
    from app.graph import routing

    state = _state("the first one")
    fake = _AddTaskLLM()
    with patch("app.models.llm", new=fake):
        result = await routing.classify_intent(state)

    assert fake.calls == 0
    assert result["intent"] == "COMPLETE"
    assert result["pending_clarification"] == state["pending_clarification"]


@pytest.mark.asyncio
async def test_log_new_reply_is_not_an_option_reference() -> None:
    from app.graph import routing

    fake = _AddTaskLLM()
    with patch("app.models.llm", new=fake):
        result = await routing.classify_intent(_state("no it's new, just log it"))

    assert fake.calls == 1
    assert result["intent"] == "ADD_TASK"
    assert result["pending_clarification"] is None


@pytest.mark.parametrize(
    "example",
    [
        '"the first one" (awaiting clarification: yes) → COMPLETE',
        '"the second one" (awaiting clarification: yes) → COMPLETE',
        '"no it\'s new, just log it" (awaiting clarification: yes) → ADD_TASK',
    ],
)
def test_classifier_prompt_carries_the_example(example: str) -> None:
    assert example in _INTENT_SYSTEM_PROMPT


def test_shared_template_mirrors_the_rule() -> None:
    from app.prompts.loader import render_with_defaults

    rendered = render_with_defaults("shared.md.j2", {})
    assert "A reply\n  that picks one of the offered options" in rendered
    assert '"the first one" (awaiting clarification: yes) → COMPLETE' in rendered
