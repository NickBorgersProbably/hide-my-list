"""Regression: a past-tense report reaches COMPLETE, never a new task.

The classifier prompt is pinned here; whether the cheap tier follows it is
scored by tests/evals/fixtures/classify_intent/past_tense_report.yaml. The
intake backstop is deterministic once the model returns `already_done`, so it
is asserted directly.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.graph.routing import _INTENT_SYSTEM_PROMPT


def test_classifier_prompt_carries_the_past_tense_rule() -> None:
    assert (
        "A past-tense report of something the user did is COMPLETE even when it names\n"
        "  something not on the list."
    ) in _INTENT_SYSTEM_PROMPT


@pytest.mark.parametrize(
    "example",
    [
        '"I also paid the gas bill!" → COMPLETE',
        '"finished that one too" → COMPLETE',
        '"What task?" → CHAT',
        '"sure" (right after the assistant suggested a task) → CHAT',
        '"ok let\'s do it" (right after the assistant suggested a task) → CHAT',
        '"I need to renew the car registration this week" → ADD_TASK',
        '"no it\'s new, just log it" (awaiting clarification: yes) → ADD_TASK',
    ],
)
def test_classifier_prompt_carries_the_example(example: str) -> None:
    assert example in _INTENT_SYSTEM_PROMPT


def test_shared_template_mirrors_the_rule() -> None:
    from app.prompts.loader import render_with_defaults

    rendered = render_with_defaults("shared.md.j2", {})
    assert "A past-tense report of something the user did is COMPLETE" in rendered
    assert '"I also paid the gas bill!" → COMPLETE' in rendered


@pytest.mark.asyncio
async def test_intake_already_done_saves_nothing_and_hands_off() -> None:
    response = MagicMock()
    response.content = json.dumps({"action": "already_done"})
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=response)
    handoff = {"pending_outbound": [{"recipient": "<test-peer>", "body": "Which one?"}]}
    complete_node = AsyncMock(return_value=handoff)
    state = {"peer": "<test-peer>", "incoming": "I also did the placeholder thing!", "messages": []}

    with (
        patch("app.models.llm", return_value=model),
        patch("app.graph.nodes.complete.complete_node", complete_node),
        patch("app.tools.notion.create_task", AsyncMock()) as create_task,
        patch("app.tools.notion.create_reminder", AsyncMock()) as create_reminder,
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(state)  # type: ignore[arg-type]

    complete_node.assert_awaited_once_with(state)
    assert result is handoff
    create_task.assert_not_awaited()
    create_reminder.assert_not_awaited()
