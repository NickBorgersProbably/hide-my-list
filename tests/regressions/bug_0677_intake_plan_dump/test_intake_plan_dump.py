"""Regression: the intake confirmation is task + when + first step.

The prompt used to require "[work type], ~[time]. Here's your plan: 1) X,
2) Y" and "This is 1 of [N] steps", and the deadline summary listed every
nudge slot. These tests pin the prompt strings and the deterministic parts of
the reply; the model's adherence is scored by
tests/evals/fixtures/intake/confirmation_one_sentence.yaml.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.prompts.loader import render_with_defaults
from app.scheduler.deadline_planner import format_reminder_summary


def _rendered_intake_prompt() -> str:
    return render_with_defaults("intake.md.j2", {})


def test_prompt_no_longer_asks_for_a_plan_labels_or_step_count() -> None:
    prompt = _rendered_intake_prompt()

    assert "Here's your plan" not in prompt
    assert "1) X, 2) Y" not in prompt
    assert "[work type], ~[time]" not in prompt
    assert "This is 1 of [N] steps" not in prompt
    assert "Got it — focus, ~30 min" not in prompt


def test_prompt_states_the_one_sentence_shape_and_the_never_list() -> None:
    prompt = _rendered_intake_prompt()

    assert "First step:" in prompt
    assert "Never put these in the confirmation" in prompt
    for banned in ("the work type", "a time estimate", "numbered plan", "a list of reminder"):
        assert banned in prompt


def test_reminder_summary_names_only_the_earliest_nudge() -> None:
    slots = [
        ("3d", datetime(2026, 6, 4, 22, tzinfo=UTC)),
        ("1d", datetime(2026, 6, 8, 22, tzinfo=UTC)),
        ("7d", datetime(2026, 6, 1, 22, tzinfo=UTC)),
        ("4h", datetime(2026, 6, 11, 18, tzinfo=UTC)),
    ]

    assert format_reminder_summary(slots, "America/Chicago") == "First nudge Mon 5pm."


@pytest.mark.asyncio
async def test_fallback_confirmation_carries_no_label_or_estimate() -> None:
    response = MagicMock()
    response.content = json.dumps({
        "action": "save",
        "title": "Placeholder task",
        "work_type": "focus",
        "urgency": 50,
        "time_estimate_minutes": 45,
        "energy_required": "Medium",
        "is_reminder": False,
        "inline_steps": "1. First step\n2. Second step",
    })
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=response)

    with (
        patch("app.models.llm", return_value=model),
        patch("app.tools.notion.query_all", AsyncMock(return_value={"results": []})),
        patch(
            "app.tools.notion.create_task",
            AsyncMock(return_value={"id": str(uuid.uuid4())}),
        ) as create_task,
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node({  # type: ignore[typeddict-item]
            "peer": "<test-peer>",
            "incoming": "placeholder task",
            "messages": [],
            "user_prefs": {},
        })

    body = result["pending_outbound"][0]["body"]
    assert body == "Got it — {task}."
    # Steps are still stored with the task; only the reply leaves them out.
    assert create_task.await_args.kwargs["inline_steps"] == "1. First step\n2. Second step"
