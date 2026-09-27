"""Bug NNNN: "actually make it 6pm" must move the reminder, not add a second one.

See README.md. Two intake turns with a mocked model: turn 1 sets a 5pm
reminder, turn 2 moves it. Turn 2 receives turn 1's recent-task ledger, as
the checkpoint delivers it in production.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.support.notion_fake import FakeNotion

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

PEER = "<test-bug-NNNN-peer>"


def _model(payload: dict[str, Any]) -> Any:
    response = MagicMock()
    response.content = json.dumps(payload)
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=response)
    return model


def _save(remind_at: str, reschedule_of: str | None, confirmation: str) -> dict[str, Any]:
    return {
        "action": "save",
        "title": "Call the pharmacy",
        "work_type": "independent",
        "urgency": 90,
        "time_estimate_minutes": 5,
        "energy_required": "Low",
        "is_reminder": True,
        "remind_at": remind_at,
        "due_at": None,
        "reschedule_of": reschedule_of,
        "use_hidden_subtasks": False,
        "sub_tasks": [],
        "inline_steps": "",
        "confirmation_message": confirmation,
        "already_finished": False,
    }


def _state(incoming: str, recent_tasks: list[Any]) -> Any:
    return {
        "peer": PEER,
        "incoming": incoming,
        "intent": "ADD_TASK",
        "messages": [],
        "active_task": None,
        "streak": 0,
        "tasks_completed_today": 0,
        "user_prefs": {},
        "mood": None,
        "available_minutes": None,
        "conversation_state": "idle",
        "pending_outbound": [],
        "recent_tasks": recent_tasks,
    }


async def _outbox(page_id: str) -> list[tuple[str, str | None, datetime]]:
    import psycopg

    async with await psycopg.AsyncConnection.connect(os.environ["DATABASE_URL"]) as conn:
        cursor = await conn.execute(
            "SELECT state, last_error, due_at FROM reminder_outbox "
            "WHERE notion_page_id = %s AND peer = %s ORDER BY created_at, due_at",
            (page_id, PEER),
        )
        return [tuple(row) for row in await cursor.fetchall()]  # type: ignore[misc]


@pytest.mark.asyncio
async def test_time_change_moves_the_reminder_instead_of_duplicating_it() -> None:
    from app.graph.nodes.intake import intake_node
    from app.tools.db import run_migrations

    run_migrations()
    fake = FakeNotion()
    undo = fake.install()
    try:
        with patch("app.models.llm", return_value=_model(_save(
            "2026-10-01T17:00:00-05:00", None, "Got it — I'll remind you at 5pm to {task}."
        ))):
            first = await intake_node(_state("remind me to call the pharmacy at 5pm", []))
        (page,) = fake.written_pages("create_reminder")

        with patch("app.models.llm", return_value=_model(_save(
            "2026-10-01T18:00:00-05:00", "R1", "Got it — I'll remind you at 6pm to {task}."
        ))):
            second = await intake_node(_state("actually make it 6pm", first["recent_tasks"]))
    finally:
        undo()

    reminder_pages = [p for p, fields in fake.pages.items() if fields.get("is_reminder")]
    assert reminder_pages == [page], "the time change created a second reminder page"
    assert fake.pages[page]["remind_at"] == "2026-10-01T23:00:00+00:00"
    assert fake.status_of(page) == "Pending"

    assert await _outbox(page) == [
        ("dead", "rescheduled by user", datetime(2026, 10, 1, 22, 0, tzinfo=UTC)),
        ("pending", None, datetime(2026, 10, 1, 23, 0, tzinfo=UTC)),
    ]

    draft = second["pending_outbound"][0]
    assert draft["notion_page_id"] == page
    assert draft["notion_page_title"] == "Call the pharmacy"
    assert [entry["page_id"] for entry in second["recent_tasks"]] == [page]
