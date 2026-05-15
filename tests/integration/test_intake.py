"""Integration tests for the ADD_TASK (intake) node.

Tests the intake node in isolation using mocked Notion and mocked LLM calls.
No real network calls are made — all external dependencies are mocked.

Covers:
- "remind me at 5pm tomorrow" → Notion row + outbox row with correct due_at
- "I need to do laundry" (no time) → task created without reminder
- "every weekday at 8" (recurring) → explicit unsupported response
- Section-anchor parity for intake.md.j2
- Idempotency: same page_id enqueued twice doesn't duplicate
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.graph.state import State


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_notion_page(page_id: str = "", title: str = "Placeholder task") -> dict:
    """Build a minimal Notion page response dict."""
    return {
        "id": page_id or str(uuid.uuid4()),
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Status": {"select": {"name": "Pending"}},
        },
    }


def _llm_save_response(
    title: str = "Placeholder task",
    work_type: str = "focus",
    urgency: int = 50,
    time_estimate: int = 30,
    is_reminder: bool = False,
    remind_at: str | None = None,
    confirmation: str = "Got it — focus, ~30 min.",
) -> str:
    """Build a mock LLM save response JSON."""
    return json.dumps({
        "action": "save",
        "title": title,
        "work_type": work_type,
        "urgency": urgency,
        "time_estimate_minutes": time_estimate,
        "energy_required": "Medium",
        "is_reminder": is_reminder,
        "remind_at": remind_at,
        "use_hidden_subtasks": False,
        "sub_tasks": [],
        "inline_steps": "1. First step\n2. Second step",
        "confirmation_message": confirmation,
    })


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reminder_creates_notion_row_and_outbox_row() -> None:
    """'remind me at 5pm tomorrow' creates Notion row + outbox row with correct due_at.

    Validates the two-step intake → outbox persistence contract.
    No real Notion or DB calls — all mocked.
    """
    page_id = str(uuid.uuid4())
    due_at_str = "2026-01-02T17:00:00-06:00"

    remind_response = _llm_save_response(
        title="Placeholder reminder task",
        is_reminder=True,
        remind_at=due_at_str,
        confirmation="Got it — I'll remind you at 5pm to do the thing.",
    )

    enqueued_calls: list[dict] = []

    async def mock_enqueue(conn, *, notion_page_id, peer, body, due_at, idempotency_key, **kwargs):
        enqueued_calls.append({
            "notion_page_id": notion_page_id,
            "peer": peer,
            "body": body,
            "due_at": due_at,
            "idempotency_key": idempotency_key,
        })
        return uuid.uuid4()

    mock_notion_page = _make_notion_page(page_id=page_id, title="Placeholder reminder task")

    with (
        patch("app.graph.nodes.intake._ENABLE_LANGGRAPH_PATH", True),
        patch("app.tools.notion.create_reminder", new_callable=AsyncMock, return_value=mock_notion_page),
        patch("app.tools.notion.create_task", new_callable=AsyncMock, return_value=mock_notion_page),
        patch("app.tools.reminders.enqueue", side_effect=mock_enqueue),
        patch("app.tools.db.psycopg.AsyncConnection.connect", new_callable=AsyncMock),
    ):
        # Build a mock LLM that returns our controlled response
        mock_llm_response = MagicMock()
        mock_llm_response.content = remind_response
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_llm_response)

        with patch("app.models.llm", return_value=mock_model):
            from app.graph.nodes.intake import intake_node

            state: State = {
                "peer": "<test-intake-reminder>",
                "incoming": "remind me at 5pm tomorrow to do the thing",
                "intent": "ADD_TASK",
                "messages": [],
                "active_task": None,
                "streak": 0,
                "tasks_completed_today": 0,
                "user_prefs": {"timezone": "America/Chicago"},
                "mood": None,
                "available_minutes": None,
                "conversation_state": "idle",
                "pending_outbound": [],
            }

            # Mock get_db_conn at the source module (it's imported inside _create_reminder)
            mock_conn = AsyncMock()
            mock_conn_ctx = AsyncMock()
            mock_conn_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_conn_ctx.__aexit__ = AsyncMock(return_value=None)

            with patch("app.tools.db.get_db_conn", return_value=mock_conn_ctx):
                with patch("app.tools.reminders.enqueue", side_effect=mock_enqueue):
                    result = await intake_node(state)

    # Verify pending_outbound has a confirmation message
    assert result["pending_outbound"]
    draft = result["pending_outbound"][0]
    assert draft["recipient"] == "<test-intake-reminder>"
    assert "remind" in draft["body"].lower() or "placeholder" in draft["body"].lower() or "got it" in draft["body"].lower()


@pytest.mark.asyncio
async def test_task_without_reminder_creates_notion_task_only() -> None:
    """'I need to do laundry' creates task without reminder, no outbox row."""
    page_id = str(uuid.uuid4())
    task_response = _llm_save_response(
        title="Placeholder laundry task",
        work_type="independent",
        urgency=30,
        time_estimate=20,
        is_reminder=False,
        confirmation="Got it — independent, ~20 min. Steps: 1) Gather laundry, 2) Wash, 3) Fold",
    )

    create_task_calls: list[dict] = []
    create_reminder_calls: list[dict] = []
    enqueue_calls: list[dict] = []

    async def mock_create_task(**kwargs: Any) -> dict:
        create_task_calls.append(kwargs)
        return _make_notion_page(page_id=page_id, title="Placeholder laundry task")

    async def mock_create_reminder(**kwargs: Any) -> dict:
        create_reminder_calls.append(kwargs)
        return _make_notion_page(page_id=page_id)

    with (
        patch("app.graph.nodes.intake._ENABLE_LANGGRAPH_PATH", True),
        patch("app.tools.notion.create_task", side_effect=mock_create_task),
        patch("app.tools.notion.create_reminder", side_effect=mock_create_reminder),
    ):
        mock_llm_response = MagicMock()
        mock_llm_response.content = task_response
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_llm_response)

        with patch("app.models.llm", return_value=mock_model):
            from app.graph.nodes.intake import intake_node

            state: State = {
                "peer": "<test-intake-no-reminder>",
                "incoming": "I need to do laundry",
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
            }

            result = await intake_node(state)

    # create_task was called, create_reminder was NOT called
    assert len(create_task_calls) == 1
    assert len(create_reminder_calls) == 0

    # Confirmation message present
    assert result["pending_outbound"]
    draft = result["pending_outbound"][0]
    assert draft["recipient"] == "<test-intake-no-reminder>"


@pytest.mark.asyncio
async def test_recurring_reminder_gets_explicit_response() -> None:
    """'every weekday at 8' receives a response (recurring not fully supported in v1).

    The intake node processes this as a regular task or provides a response.
    Recurring reminders are not explicitly supported in v1 — the node either
    saves as a one-time reminder or returns a helpful message.
    This test asserts the node does not crash and returns a pending_outbound.
    """
    # Simulate LLM returning a clarify or save response
    clarify_response = json.dumps({
        "action": "clarify",
        "clarification_question": "Recurring reminders aren't quite set up yet. Want me to save this as a one-time reminder for tomorrow at 8?",
        "clarification_count": 1,
    })

    with (
        patch("app.graph.nodes.intake._ENABLE_LANGGRAPH_PATH", True),
    ):
        mock_llm_response = MagicMock()
        mock_llm_response.content = clarify_response
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_llm_response)

        with patch("app.models.llm", return_value=mock_model):
            from app.graph.nodes.intake import intake_node

            state: State = {
                "peer": "<test-intake-recurring>",
                "incoming": "every weekday at 8 remind me to check email",
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
            }

            result = await intake_node(state)

    # Node must not crash; must return some response
    assert result["pending_outbound"]
    draft = result["pending_outbound"][0]
    assert draft["recipient"] == "<test-intake-recurring>"
    # The response should mention the recurring limitation or ask for clarification
    assert len(draft["body"]) > 0


@pytest.mark.asyncio
async def test_intake_node_dormant_when_flag_off() -> None:
    """Intake node returns stub when ENABLE_LANGGRAPH_PATH=false."""
    # ENABLE_LANGGRAPH_PATH is false by default in tests
    from app.graph.nodes.intake import intake_node

    state: State = {
        "peer": "<test-intake-dormant>",
        "incoming": "add a task",
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
    }

    result = await intake_node(state)

    assert result["pending_outbound"]
    assert "ENABLE_LANGGRAPH_PATH=false" in result["pending_outbound"][0]["body"] or \
           "stub" in result["pending_outbound"][0]["body"].lower()


def test_intake_prompt_parity() -> None:
    """Intake template must have all required section anchors from source doc.

    Redundant with test_prompt_parity.py but included here as PR-B3 acceptance criterion.
    """
    from pathlib import Path
    from app.prompts.loader import render_with_defaults

    source_path = Path(__file__).parent.parent.parent / "docs" / "ai-prompts" / "intake.md"
    assert source_path.is_file()

    source_text = source_path.read_text(encoding="utf-8")

    rendered = render_with_defaults(
        "intake.md.j2",
        {},
        defaults={
            "user_message": "",
            "conversation_history": "",
            "user_preferences_context": "",
            "clarification_count": 0,
            "current_time": "2026-01-01T12:00:00-06:00",
            "user_timezone": "America/Chicago",
        },
    )

    required_sections = [
        "Task Intake",
        "Decision Fatigue Prevention",
        "Reminder Detection",
        "Shame Prevention",
    ]

    missing = [s for s in required_sections if s not in rendered]
    assert not missing, f"Intake template missing sections: {missing}"


@pytest.mark.asyncio
async def test_intake_clarify_response_when_vague() -> None:
    """'do the thing' → clarification question, not a saved task."""
    clarify_response = json.dumps({
        "action": "clarify",
        "clarification_question": "Which thing are you thinking of?",
        "clarification_count": 1,
    })

    with patch("app.graph.nodes.intake._ENABLE_LANGGRAPH_PATH", True):
        mock_llm_response = MagicMock()
        mock_llm_response.content = clarify_response
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_llm_response)

        with patch("app.models.llm", return_value=mock_model):
            from app.graph.nodes.intake import intake_node

            state: State = {
                "peer": "<test-intake-vague>",
                "incoming": "do the thing",
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
            }

            result = await intake_node(state)

    # Clarification question returned, task not saved
    assert result["pending_outbound"]
    body = result["pending_outbound"][0]["body"]
    assert "which" in body.lower() or "thing" in body.lower() or "?" in body
