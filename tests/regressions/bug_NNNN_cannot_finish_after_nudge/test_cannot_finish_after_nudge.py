"""Bug NNNN: CANNOT_FINISH right after a deadline nudge must be about the nudged task.

See README.md. No active task; the only anchor is the `nudged` ledger entry
`hydrate_context` merges from the delivery.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.support.notion_fake import FakeNotion


def _model(payload: dict[str, Any]) -> Any:
    response = MagicMock()
    response.content = json.dumps(payload)
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=response)
    return model


def _state(page_id: str, incoming: str) -> Any:
    return {
        "peer": "<test-bug-NNNN-peer>",
        "incoming": incoming,
        "intent": "CANNOT_FINISH",
        "messages": [],
        "active_task": None,
        "streak": 0,
        "tasks_completed_today": 0,
        "user_prefs": {},
        "mood": None,
        "available_minutes": None,
        "conversation_state": "idle",
        "pending_outbound": [],
        "recent_tasks": [{
            "page_id": page_id,
            "title": "Renew the car registration",
            "kind": "task",
            "event": "nudged",
            "at": datetime.now(UTC).isoformat(),
        }],
    }


@pytest.mark.asyncio
async def test_cannot_finish_after_a_nudge_names_the_nudged_task() -> None:
    from app.graph.nodes.cannot_finish import cannot_finish_node

    fake = FakeNotion()
    page = fake.seed_task(title="Renew the car registration", time_estimate=45)
    model = _model({"phase": "ask_progress", "progress_question": "No worries — where'd you get to?"})
    undo = fake.install()
    try:
        with patch("app.models.llm", return_value=model):
            result = await cannot_finish_node(_state(page, "I can't finish that today"))
    finally:
        undo()

    prompt = str(model.ainvoke.await_args.args[0][0].content)
    assert "CURRENT TASK: Renew the car registration" in prompt
    draft = result["pending_outbound"][0]
    assert draft["notion_page_id"] == page
    assert draft["notion_page_title"] == "Renew the car registration"


@pytest.mark.asyncio
async def test_cannot_finish_after_a_nudge_writes_sub_tasks_under_the_nudged_page() -> None:
    from app.graph.nodes.cannot_finish import cannot_finish_node

    fake = FakeNotion()
    page = fake.seed_task(title="Renew the car registration")
    model = _model({
        "phase": "analyze_remaining",
        "completed_portion": "found the renewal form",
        "remaining_sub_tasks": [
            {"title": "Fill in the renewal form", "time_estimate_minutes": 20, "sequence": 1},
            {"title": "Pay the renewal fee online", "time_estimate_minutes": 15, "sequence": 2},
        ],
        "next_sub_task_message": "Finding the form counts. Next: fill it in, ~20 min, whenever.",
    })
    undo = fake.install()
    try:
        with patch("app.models.llm", return_value=model):
            await cannot_finish_node(
                _state(page, "I can't finish that today, I only found the form")
            )
    finally:
        undo()

    children = [
        fake.pages[w.page_id] for w in fake.writes if w.op == "create_task"
    ]
    assert [(c["title"], c["parent_id"]) for c in children] == [
        ("Fill in the renewal form", page),
        ("Pay the renewal fee online", page),
    ]
