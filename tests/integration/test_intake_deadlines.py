"""Integration tests for deadline detection and inline reminder scheduling in intake.

Tests the intake node + reminder_scheduling.schedule_for_task integration:
- Deadline phrases produce correct due_at in Notion create_task calls
- Inline reminder scheduling writes ledger + outbox rows
- Confirmation message includes reminder summary
- Stakes detection triggers clarification on high-stakes tasks
- Low-stakes tasks with no deadline save silently with disclaimer
- Inline enqueue failure → ops_alert + task still saved + no mark_reminder_scheduled

Notion + DB calls are mocked. No DATABASE_URL required for most tests;
tests that exercise real ledger writes are skipped if DATABASE_URL absent.

Private data: all values are placeholders / synthetic.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.graph.state import State

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_notion_page(page_id: str = "", title: str = "Placeholder task") -> dict:
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
    due_at: str | None = None,
    is_high_stakes: bool = False,
    confirmation: str = "Saved — focus, ~30 min.",
) -> str:
    return json.dumps({
        "action": "save",
        "title": title,
        "work_type": work_type,
        "urgency": urgency,
        "time_estimate_minutes": time_estimate,
        "energy_required": "Medium",
        "is_reminder": is_reminder,
        "remind_at": remind_at,
        "due_at": due_at,
        "is_high_stakes": is_high_stakes,
        "use_hidden_subtasks": False,
        "sub_tasks": [],
        "inline_steps": "",
        "confirmation_message": confirmation,
    })


def _llm_clarify_response(question: str) -> str:
    return json.dumps({
        "action": "clarify",
        "clarification_question": question,
        "clarification_count": 1,
    })


def _state(peer: str = "<test-peer>", incoming: str = "placeholder", prefs: dict | None = None) -> State:
    return {
        "peer": peer,
        "incoming": incoming,
        "intent": "ADD_TASK",
        "messages": [],
        "active_task": None,
        "streak": 0,
        "tasks_completed_today": 0,
        "user_prefs": prefs or {"timezone": "America/Chicago"},
        "mood": None,
        "available_minutes": None,
        "conversation_state": "idle",
        "pending_outbound": [],
    }


def _mock_llm(response_text: str):
    """Return a patcher for the LLM that yields the given response text."""
    mock_llm_response = MagicMock()
    mock_llm_response.content = response_text
    mock_model = AsyncMock()
    mock_model.ainvoke = AsyncMock(return_value=mock_llm_response)
    return mock_model


# ---------------------------------------------------------------------------
# Test: deadline task creates notion row with due_at_iso
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deadline_phrase_creates_task_with_due_at() -> None:
    """'finish report by Friday' → create_task called with due_at_iso set.

    The LLM returns a due_at in its JSON; intake passes it to notion.create_task.
    """
    page_id = str(uuid.uuid4())
    due_at_str = "2026-06-05T17:00:00-05:00"  # Fri 17:00 Central

    task_response = _llm_save_response(
        title="Placeholder report task",
        work_type="focus",
        urgency=60,
        due_at=due_at_str,
        confirmation="Saved — focus, ~30 min, due Fri.",
    )

    create_task_calls: list[dict] = []

    async def mock_create_task(**kwargs: Any) -> dict:
        create_task_calls.append(kwargs)
        return _make_notion_page(page_id=page_id, title="Placeholder report task")

    with (
        patch("app.tools.notion.create_task", side_effect=mock_create_task),
        patch("app.tools.notion.create_reminder", new_callable=AsyncMock),
        patch("app.tools.notion.mark_reminder_scheduled", new_callable=AsyncMock),
        patch(
            "app.scheduler.reminder_scheduling.schedule_for_task",
            new_callable=AsyncMock,
            return_value=([], []),
        ),
        patch("app.models.llm", return_value=_mock_llm(task_response)),
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(
            _state(
                peer="<test-deadline-peer>",
                incoming="finish placeholder report by Friday",
            )
        )

    # create_task was called with the correct due_at_iso
    assert len(create_task_calls) == 1
    assert create_task_calls[0]["due_at_iso"] == due_at_str

    # Confirmation is present
    assert result["pending_outbound"]
    draft = result["pending_outbound"][0]
    assert draft["recipient"] == "<test-deadline-peer>"


@pytest.mark.asyncio
async def test_clock_time_deadline_sets_exact_time() -> None:
    """'finish report by 10am tomorrow' → due_at_iso reflects exact clock time."""
    page_id = str(uuid.uuid4())
    due_at_str = "2026-06-02T10:00:00-05:00"  # 10am Central tomorrow

    task_response = _llm_save_response(
        title="Placeholder report task",
        work_type="focus",
        urgency=70,
        due_at=due_at_str,
        confirmation="Saved — focus, ~30 min, due tomorrow 10am.",
    )

    create_task_calls: list[dict] = []

    async def mock_create_task(**kwargs: Any) -> dict:
        create_task_calls.append(kwargs)
        return _make_notion_page(page_id=page_id)

    with (
        patch("app.tools.notion.create_task", side_effect=mock_create_task),
        patch("app.tools.notion.mark_reminder_scheduled", new_callable=AsyncMock),
        patch(
            "app.scheduler.reminder_scheduling.schedule_for_task",
            new_callable=AsyncMock,
            return_value=([], []),
        ),
        patch("app.models.llm", return_value=_mock_llm(task_response)),
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(
            _state(
                peer="<test-clock-deadline-peer>",
                incoming="finish placeholder report by 10am tomorrow",
            )
        )

    assert len(create_task_calls) == 1
    assert create_task_calls[0]["due_at_iso"] == due_at_str
    assert result["pending_outbound"]


@pytest.mark.asyncio
async def test_deadline_task_confirmation_includes_reminder_summary() -> None:
    """Deadline task with successful inline scheduling → confirmation includes reminder summary."""
    page_id = str(uuid.uuid4())
    due_at_str = "2026-06-05T17:00:00-05:00"

    task_response = _llm_save_response(
        title="Placeholder task",
        due_at=due_at_str,
        urgency=60,
        confirmation="Saved — focus, ~30 min, due Fri.",
    )

    # Simulate successful schedule_for_task returning assigned slots
    fri_5pm = datetime(2026, 6, 5, 17, 0, tzinfo=UTC)
    wed_9am = datetime(2026, 6, 3, 14, 0, tzinfo=UTC)  # UTC → 9am Central
    mock_assigned_slots = [("3d", wed_9am), ("4h", fri_5pm - __import__('datetime').timedelta(hours=4))]

    async def mock_schedule(**kwargs: Any) -> tuple:
        return (mock_assigned_slots, [])

    with (
        patch("app.tools.notion.create_task", new_callable=AsyncMock, return_value=_make_notion_page(page_id=page_id)),
        patch("app.tools.notion.mark_reminder_scheduled", new_callable=AsyncMock),
        patch("app.scheduler.reminder_scheduling.schedule_for_task", side_effect=mock_schedule),
        patch("app.models.llm", return_value=_mock_llm(task_response)),
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(
            _state(peer="<test-summary-peer>", incoming="placeholder task by Friday")
        )

    assert result["pending_outbound"]
    body = result["pending_outbound"][0]["body"]
    # The body should contain the LLM confirmation + the reminder summary
    assert "Saved" in body
    assert "I'll ping you" in body


@pytest.mark.asyncio
async def test_high_stakes_no_deadline_triggers_clarification() -> None:
    """'I need to purchase life insurance' → clarification question returned."""
    clarify_response = _llm_clarify_response(
        "When do you want this done by?"
    )

    with patch("app.models.llm", return_value=_mock_llm(clarify_response)):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(
            _state(
                peer="<test-stakes-peer>",
                incoming="I need to purchase life insurance",
            )
        )

    # Should return a clarification question, not save
    assert result["pending_outbound"]
    body = result["pending_outbound"][0]["body"]
    assert "?" in body
    assert "when" in body.lower() or "done by" in body.lower()


@pytest.mark.asyncio
async def test_low_stakes_no_deadline_saves_silently_with_disclaimer() -> None:
    """'organize my bookshelf' → saved silently, confirmation includes no-deadline disclaimer."""
    page_id = str(uuid.uuid4())

    task_response = _llm_save_response(
        title="Placeholder organize task",
        work_type="independent",
        urgency=30,
        due_at=None,
        is_high_stakes=False,
        confirmation="Saved — independent, ~20 min.",
    )

    create_task_calls: list[dict] = []

    async def mock_create_task(**kwargs: Any) -> dict:
        create_task_calls.append(kwargs)
        return _make_notion_page(page_id=page_id, title="Placeholder organize task")

    with (
        patch("app.tools.notion.create_task", side_effect=mock_create_task),
        patch("app.models.llm", return_value=_mock_llm(task_response)),
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(
            _state(
                peer="<test-no-deadline-peer>",
                incoming="organize my placeholder bookshelf",
            )
        )

    # Task saved without clarification
    assert len(create_task_calls) == 1
    assert create_task_calls[0]["due_at_iso"] is None

    # Confirmation has the no-deadline disclaimer
    assert result["pending_outbound"]
    body = result["pending_outbound"][0]["body"]
    assert "No deadline" in body or "won't ping" in body or "remind me" in body.lower()


@pytest.mark.asyncio
async def test_inline_enqueue_failure_task_still_saved_ops_alert_emitted() -> None:
    """Inline reminder enqueue failure → task still saved; ops_alert emitted; no mark_reminder_scheduled."""
    page_id = str(uuid.uuid4())
    due_at_str = "2026-06-05T17:00:00-05:00"

    task_response = _llm_save_response(
        title="Placeholder task with deadline",
        due_at=due_at_str,
        urgency=60,
        confirmation="Saved — focus, ~30 min, due Fri.",
    )

    ops_alert_calls: list[dict] = []

    async def mock_ops_enqueue(kind: str, body: str, severity: str = "warning") -> uuid.UUID:
        ops_alert_calls.append({"kind": kind, "body": body, "severity": severity})
        return uuid.uuid4()

    mark_scheduled_calls: list = []

    async def mock_mark_scheduled(pid: str) -> dict:
        mark_scheduled_calls.append(pid)
        return {}

    # schedule_for_task returns one failure
    async def mock_schedule(**kwargs: Any) -> tuple:
        return ([], ["4h"])  # empty assigned, "4h" failed

    with (
        patch("app.tools.notion.create_task", new_callable=AsyncMock, return_value=_make_notion_page(page_id=page_id)),
        patch("app.tools.notion.mark_reminder_scheduled", side_effect=mock_mark_scheduled),
        patch("app.tools.ops_alerts.enqueue", side_effect=mock_ops_enqueue),
        patch("app.scheduler.reminder_scheduling.schedule_for_task", side_effect=mock_schedule),
        patch("app.models.llm", return_value=_mock_llm(task_response)),
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(
            _state(peer="<test-failure-peer>", incoming="placeholder task by Friday")
        )

    # Task was saved (pending_outbound exists)
    assert result["pending_outbound"]
    body = result["pending_outbound"][0]["body"]
    # Confirmation body mentions retry
    assert "retry" in body.lower() or "tonight" in body.lower() or "couldn't" in body.lower()

    # ops_alert was emitted for the partial failure
    assert any("partial" in call["kind"] or "failure" in call["kind"] for call in ops_alert_calls)

    # mark_reminder_scheduled was NOT called (Reminder Scheduled At stays empty for daemon)
    assert len(mark_scheduled_calls) == 0


@pytest.mark.asyncio
async def test_no_deadline_task_no_schedule_for_task_called() -> None:
    """Task with no due_at → schedule_for_task is never called."""
    page_id = str(uuid.uuid4())

    task_response = _llm_save_response(
        title="Placeholder no-deadline task",
        due_at=None,
        is_high_stakes=False,
        confirmation="Saved — focus, ~30 min.",
    )

    schedule_calls: list = []

    async def mock_schedule(**kwargs: Any) -> tuple:
        schedule_calls.append(kwargs)
        return ([], [])

    with (
        patch("app.tools.notion.create_task", new_callable=AsyncMock, return_value=_make_notion_page(page_id=page_id)),
        patch("app.scheduler.reminder_scheduling.schedule_for_task", side_effect=mock_schedule),
        patch("app.models.llm", return_value=_mock_llm(task_response)),
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(
            _state(peer="<test-no-sched-peer>", incoming="placeholder task no deadline")
        )

    assert result["pending_outbound"]
    # schedule_for_task was not called since there's no due_at
    assert len(schedule_calls) == 0
