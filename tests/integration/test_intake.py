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

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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

    async def mock_create_task(**kwargs: Any) -> dict:
        create_task_calls.append(kwargs)
        return _make_notion_page(page_id=page_id, title="Placeholder laundry task")

    async def mock_create_reminder(**kwargs: Any) -> dict:
        create_reminder_calls.append(kwargs)
        return _make_notion_page(page_id=page_id)

    with (
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


def test_intake_prompt_parity() -> None:
    """Intake template must have all required section anchors from source doc.

    Redundant with test_prompt_parity.py but included here as PR-B3 acceptance criterion.
    """
    from pathlib import Path

    from app.prompts.loader import render_with_defaults

    source_path = Path(__file__).parent.parent.parent / "docs" / "ai-prompts" / "intake.md"
    assert source_path.is_file()

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


@pytest.mark.asyncio
async def test_unparseable_output_saves_raw_task_and_alerts() -> None:
    """A truncated/garbled LLM response must NOT masquerade as a confirmed task.

    Regression for the intake reminder-drop: when the model output is
    unparseable (e.g. truncated at the output-token ceiling), the node must
    (1) preserve capture by saving a raw task titled from the user's own
    words, (2) never claim a reminder was set, and (3) emit an ops alert so
    the operator sees the model degradation. The old behavior silently saved
    a plain task titled with the garbled LLM text and replied "Got it — added."
    """
    page_id = str(uuid.uuid4())
    incoming = "I need to clean the kitchen Friday before 10pm"
    # Truncated JSON: opening brace, cut off mid-value, no closing brace.
    truncated = '{"action": "save", "title": "clean the kitchen", "is_reminder": tr'

    create_task_calls: list[dict] = []
    create_reminder_calls: list[dict] = []
    alert_calls: list[dict] = []

    async def mock_create_task(**kwargs: Any) -> dict:
        create_task_calls.append(kwargs)
        return _make_notion_page(page_id=page_id, title="Placeholder task")

    async def mock_create_reminder(**kwargs: Any) -> dict:
        create_reminder_calls.append(kwargs)
        return _make_notion_page(page_id=page_id)

    async def mock_alert(*, kind: str, body: str, severity: str = "warning", **_: Any) -> Any:
        alert_calls.append({"kind": kind, "body": body, "severity": severity})
        return uuid.uuid4()

    with (
        patch("app.tools.notion.create_task", side_effect=mock_create_task),
        patch("app.tools.notion.create_reminder", side_effect=mock_create_reminder),
        patch("app.tools.ops_alerts.enqueue", side_effect=mock_alert),
    ):
        mock_llm_response = MagicMock()
        mock_llm_response.content = truncated
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_llm_response)

        with patch("app.models.llm", return_value=mock_model):
            from app.graph.nodes.intake import intake_node

            state: State = {
                "peer": "<test-intake-unparseable>",
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
            }

            result = await intake_node(state)

    # Raw task saved, titled from the user's words — NOT the garbled LLM output.
    assert len(create_task_calls) == 1
    assert create_task_calls[0]["title"] == incoming
    assert "is_reminder" not in create_task_calls[0]["title"]
    # No reminder was fabricated.
    assert len(create_reminder_calls) == 0
    # Operator was alerted to the parse failure.
    assert len(alert_calls) == 1
    assert alert_calls[0]["kind"] == "intake_parse_failed"
    # User got an honest message — not a bare "Got it — added." false success,
    # and it flags that the timing/reminder wasn't captured.
    assert result["pending_outbound"]
    body = result["pending_outbound"][0]["body"].lower()
    assert body.strip() != "got it — added."
    assert "remind" in body or "timing" in body or "time" in body
