"""Integration tests for intake deadline-detection + inline scheduling.

Mocks Notion + the scheduling helper at the boundary. These tests are HTTP-only
and do NOT require DATABASE_URL — schedule_for_task is exercised against a
real Postgres in tests/integration/test_reminder_scheduling.py.

Covers the PR 601 blockers in intake:
  - psy-001: confirmation must NOT append infra/retry state on failure.
  - sec-001: privacy-safe failure logging (no raw value, no exception text).
  - Successful path: deterministic reminder summary appended to confirmation.

Private data: all identifiers are placeholders.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.graph.state import State


def _make_notion_page(page_id: str = "", title: str = "Task placeholder") -> dict:
    return {
        "id": page_id or str(uuid.uuid4()),
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Status": {"select": {"name": "Pending"}},
        },
    }


def _llm_response(
    *,
    title: str = "Task placeholder",
    due_at: str | None = None,
    confirmation: str = "Got it — focus, ~30 min.",
) -> str:
    return json.dumps({
        "action": "save",
        "title": title,
        "work_type": "focus",
        "urgency": 50,
        "time_estimate_minutes": 30,
        "energy_required": "Medium",
        "is_reminder": False,
        "remind_at": None,
        "due_at": due_at,
        "use_hidden_subtasks": False,
        "sub_tasks": [],
        "inline_steps": "",
        "confirmation_message": confirmation,
    })


def _state(incoming: str) -> State:
    return {
        "peer": "<test-peer>",
        "incoming": incoming,
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


# ---------------------------------------------------------------------------
# Test 1 — successful deadline: confirmation gets the reminder summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deadline_appends_reminder_summary_to_confirmation() -> None:
    """When schedule_for_task succeeds, the confirmation must include the
    deterministic reminder summary ("I'll ping you ..."). No infra phrases."""
    page_id = str(uuid.uuid4())
    due_at_iso = (datetime.now(UTC) + timedelta(days=10)).isoformat()

    captured_summary_slot = (datetime.now(UTC) + timedelta(days=7))

    async def fake_schedule(*args: Any, **kwargs: Any) -> tuple[list[Any], list[str]]:
        return [("3d", captured_summary_slot)], []

    with (
        patch(
            "app.tools.notion.create_task",
            new_callable=AsyncMock,
            return_value=_make_notion_page(page_id=page_id),
        ),
        patch(
            "app.tools.notion.create_reminder",
            new_callable=AsyncMock,
            return_value=_make_notion_page(page_id=page_id),
        ),
        patch(
            "app.tools.notion.mark_reminder_scheduled",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "app.scheduler.reminder_scheduling.schedule_for_task",
            side_effect=fake_schedule,
        ),
    ):
        mock_llm_response = MagicMock()
        mock_llm_response.content = _llm_response(
            due_at=due_at_iso,
            confirmation="Got it — focus, ~30 min.",
        )
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_llm_response)

        with patch("app.models.llm", return_value=mock_model):
            from app.graph.nodes.intake import intake_node
            result = await intake_node(_state("finish report by Friday"))

    assert result["pending_outbound"]
    body = result["pending_outbound"][0]["body"]
    assert "Got it" in body
    assert "ping you" in body, (
        f"Expected reminder summary in confirmation, got: {body!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — psy-001: scheduling failure leaves no infra leak in confirmation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_scheduling_does_not_leak_retry_state_to_user() -> None:
    """When schedule_for_task fails, the confirmation MUST NOT mention
    "retry", "tonight", "couldn't schedule", or any infra state. The daemon
    backstop silently picks the task up — the user sees a clean acknowledgment.

    Guards psy-001 from closed PR #601.
    """
    page_id = str(uuid.uuid4())
    due_at_iso = (datetime.now(UTC) + timedelta(days=10)).isoformat()

    async def failing_schedule(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated DB failure")

    with (
        patch(
            "app.tools.notion.create_task",
            new_callable=AsyncMock,
            return_value=_make_notion_page(page_id=page_id),
        ),
        patch(
            "app.tools.notion.create_reminder",
            new_callable=AsyncMock,
            return_value=_make_notion_page(page_id=page_id),
        ),
        patch(
            "app.scheduler.reminder_scheduling.schedule_for_task",
            side_effect=failing_schedule,
        ),
    ):
        mock_llm_response = MagicMock()
        mock_llm_response.content = _llm_response(
            due_at=due_at_iso,
            confirmation="Got it — focus, ~30 min.",
        )
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_llm_response)

        with patch("app.models.llm", return_value=mock_model):
            from app.graph.nodes.intake import intake_node
            result = await intake_node(_state("finish report by Friday"))

    body = result["pending_outbound"][0]["body"]
    # Banned phrases (all from PR 601 / shared.md no-tool-narration contract).
    banned = [
        "couldn't schedule",
        "could not schedule",
        "retry",
        "tonight",
        "tool",
        "outbox",
        "database",
        "backstop",
        "scheduler",
    ]
    body_lower = body.lower()
    for phrase in banned:
        assert phrase not in body_lower, (
            f"Confirmation leaked infra/retry phrase {phrase!r}: {body!r}"
        )


# ---------------------------------------------------------------------------
# Test 3 — sec-001: malformed due_at does NOT leak raw value or exception text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_due_at_does_not_log_raw_value_or_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the LLM returns an unparseable due_at, the parse failure must
    log only flags (no raw value, no exception string). The user-controlled
    text could contain private task content per DEV-AGENTS.md.

    Guards sec-001 from closed PR #601.
    """
    page_id = str(uuid.uuid4())
    # The "raw value" the LLM returned — pretend it contains a private fragment
    # we must not echo to logs.
    malformed_due_at = "PRIVATE_FRAGMENT_FRIDAY"

    with (
        patch(
            "app.tools.notion.create_task",
            new_callable=AsyncMock,
            return_value=_make_notion_page(page_id=page_id),
        ),
        patch(
            "app.tools.notion.create_reminder",
            new_callable=AsyncMock,
            return_value=_make_notion_page(page_id=page_id),
        ),
    ):
        mock_llm_response = MagicMock()
        mock_llm_response.content = _llm_response(
            due_at=malformed_due_at,
            confirmation="Got it — focus, ~30 min.",
        )
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_llm_response)

        with patch("app.models.llm", return_value=mock_model):
            from app.graph.nodes.intake import intake_node
            with caplog.at_level(logging.DEBUG):
                result = await intake_node(_state("finish report by Friday"))

    # Confirmation succeeded — parse failure does not crash intake.
    assert result["pending_outbound"]

    # Log output must NOT contain the raw value.
    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert malformed_due_at not in log_text, (
        f"Logs leaked the raw malformed due_at value: {malformed_due_at!r}"
    )
    # And must NOT contain a Python ValueError reflection that echoes it.
    assert "fromisoformat" not in log_text, (
        "Logs leaked fromisoformat exception text which can echo the input"
    )


# ---------------------------------------------------------------------------
# Test 4 — no deadline: existing behavior preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_deadline_no_summary_no_scheduling() -> None:
    """When the LLM does not return a due_at, intake must not call
    schedule_for_task and must not append a reminder summary."""
    page_id = str(uuid.uuid4())

    schedule_called = AsyncMock(return_value=([], []))

    with (
        patch(
            "app.tools.notion.create_task",
            new_callable=AsyncMock,
            return_value=_make_notion_page(page_id=page_id),
        ),
        patch(
            "app.tools.notion.create_reminder",
            new_callable=AsyncMock,
            return_value=_make_notion_page(page_id=page_id),
        ),
        patch(
            "app.scheduler.reminder_scheduling.schedule_for_task",
            side_effect=schedule_called,
        ),
    ):
        mock_llm_response = MagicMock()
        mock_llm_response.content = _llm_response(
            due_at=None,
            confirmation="Got it — focus, ~30 min.",
        )
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_llm_response)

        with patch("app.models.llm", return_value=mock_model):
            from app.graph.nodes.intake import intake_node
            result = await intake_node(_state("I need to organize my bookshelf"))

    schedule_called.assert_not_awaited()
    body = result["pending_outbound"][0]["body"]
    assert "ping you" not in body.lower()
