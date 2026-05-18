"""Integration tests for remaining 5 intent nodes: REJECT, CANNOT_FINISH, NEED_HELP, CHECK_IN, COMPLETE.

All use mocked Notion and mocked LLM. No real network calls.

Covers:
- Each node's happy path
- Section-anchor parity for each node's prompt template
- Dormant behavior (ENABLE_LANGGRAPH_PATH=false)
- CHECK_IN APScheduler job integration
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.graph.state import State


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_llm_response(content: str) -> Any:
    """Build a mock LLM response with the given content string."""
    response = MagicMock()
    response.content = content
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=response)
    return model


def _active_task(title: str = "Placeholder active task", page_id: str = "") -> dict:
    return {
        "page_id": page_id or str(uuid.uuid4()),
        "title": title,
        "status": "In Progress",
        "work_type": "focus",
        "urgency": 60,
        "time_estimate": 45,
        "energy_required": "Medium",
    }


# ---------------------------------------------------------------------------
# REJECT node tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rejection_node_returns_alternative() -> None:
    """REJECT node returns shame-safe response with alternative task."""
    rejection_response = json.dumps({
        "rejection_category": "mood_mismatch",
        "task_update": {"rejection_count_increment": 1, "rejection_note": "not in mood"},
        "alternative_task_id": str(uuid.uuid4()),
        "user_message": "Fair enough — that tells me what kind of work fits right now. How about something lighter?",
    })

    with (
        patch("app.graph.nodes.rejection._ENABLE_LANGGRAPH_PATH", True),
        patch("app.tools.notion.query_pending", new_callable=AsyncMock, return_value={"results": []}),
        patch("app.tools.notion.update_property", new_callable=AsyncMock),
        patch("app.models.llm", return_value=_mock_llm_response(rejection_response)),
    ):
        from app.graph.nodes.rejection import rejection_node

        state: State = {
            "peer": "<test-reject>",
            "incoming": "not in the mood for that",
            "intent": "REJECT",
            "messages": [],
            "active_task": _active_task("Placeholder task to reject"),
            "streak": 0,
            "tasks_completed_today": 0,
            "user_prefs": {},
            "mood": "tired",
            "available_minutes": 20,
            "conversation_state": "selection",
            "pending_outbound": [],
        }

        result = await rejection_node(state)

    assert result["pending_outbound"]
    draft = result["pending_outbound"][0]
    assert draft["recipient"] == "<test-reject>"
    assert len(draft["body"]) > 0
    # Shame safety: response should not contain banned phrases
    body_lower = draft["body"].lower()
    banned = ["you didn't", "you should have", "you forgot", "you failed"]
    for phrase in banned:
        assert phrase not in body_lower, f"Shame phrase found: {phrase!r}"


@pytest.mark.asyncio
async def test_rejection_node_dormant_when_flag_off() -> None:
    """REJECT node returns stub when ENABLE_LANGGRAPH_PATH=false."""
    from app.graph.nodes.rejection import rejection_node

    state: State = {
        "peer": "<test-reject-dormant>",
        "incoming": "no",
        "intent": "REJECT",
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

    result = await rejection_node(state)
    assert result["pending_outbound"]
    assert "ENABLE_LANGGRAPH_PATH=false" in result["pending_outbound"][0]["body"] or \
           "stub" in result["pending_outbound"][0]["body"].lower()


def test_rejection_prompt_parity() -> None:
    """rejection.md.j2 must contain all required sections from source doc."""
    from pathlib import Path
    from app.prompts.loader import render_with_defaults

    rendered = render_with_defaults(
        "rejection.md.j2",
        {},
        defaults={
            "task_title": "placeholder",
            "rejection_reason": "",
            "remaining_tasks_json": "[]",
            "available_minutes": 30,
            "mood": "neutral",
        },
    )

    for section in ["Rejection Handling", "Rejection Categories", "Shame Prevention"]:
        assert section in rendered, f"Missing section: {section}"


# ---------------------------------------------------------------------------
# CANNOT_FINISH node tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cannot_finish_node_asks_progress() -> None:
    """CANNOT_FINISH node asks what was accomplished (shame-safe question)."""
    cannot_finish_response = json.dumps({
        "phase": "ask_progress",
        "user_message": "No worries — you figured out it's bigger than it seemed. What did you get into?",
        "progress_question": "No worries — you figured out it's bigger than it seemed. What did you get into?",
    })

    with (
        patch("app.graph.nodes.cannot_finish._ENABLE_LANGGRAPH_PATH", True),
        patch("app.models.llm", return_value=_mock_llm_response(cannot_finish_response)),
    ):
        from app.graph.nodes.cannot_finish import cannot_finish_node

        state: State = {
            "peer": "<test-cannot-finish>",
            "incoming": "this is too big, I can't finish it",
            "intent": "CANNOT_FINISH",
            "messages": [],
            "active_task": _active_task("Placeholder large task"),
            "streak": 0,
            "tasks_completed_today": 0,
            "user_prefs": {},
            "mood": None,
            "available_minutes": None,
            "conversation_state": "active",
            "pending_outbound": [],
        }

        result = await cannot_finish_node(state)

    assert result["pending_outbound"]
    draft = result["pending_outbound"][0]
    assert draft["recipient"] == "<test-cannot-finish>"
    body_lower = draft["body"].lower()
    # Response should acknowledge, not shame
    banned = ["you didn't", "you should have", "you forgot", "you failed"]
    for phrase in banned:
        assert phrase not in body_lower


def test_cannot_finish_prompt_parity() -> None:
    """cannot_finish.md.j2 must contain all required sections."""
    from app.prompts.loader import render_with_defaults

    rendered = render_with_defaults(
        "cannot_finish.md.j2",
        {},
        defaults={
            "task_title": "placeholder",
            "time_estimate": 60,
            "user_message": "",
        },
    )

    for section in ["Cannot Finish Handling", "Shame Prevention"]:
        assert section in rendered, f"Missing section: {section}"


# ---------------------------------------------------------------------------
# NEED_HELP node tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_need_help_node_provides_micro_action() -> None:
    """NEED_HELP node provides actionable guidance matched to stuck user."""
    help_response = json.dumps({
        "detected_confidence": "stuck",
        "response_level": "micro_action",
        "immediate_action": "Open the document",
        "user_message": "Let's make this tiny. Just open the document right now. That's it.",
        "encouragement": "Starting is the hardest part.",
    })

    with (
        patch("app.graph.nodes.need_help._ENABLE_LANGGRAPH_PATH", True),
        patch("app.models.llm", return_value=_mock_llm_response(help_response)),
    ):
        from app.graph.nodes.need_help import need_help_node

        state: State = {
            "peer": "<test-need-help>",
            "incoming": "I'm stuck, don't know where to start",
            "intent": "NEED_HELP",
            "messages": [],
            "active_task": _active_task("Placeholder task needing help"),
            "streak": 0,
            "tasks_completed_today": 0,
            "user_prefs": {},
            "mood": None,
            "available_minutes": None,
            "conversation_state": "active",
            "pending_outbound": [],
        }

        result = await need_help_node(state)

    assert result["pending_outbound"]
    draft = result["pending_outbound"][0]
    assert draft["recipient"] == "<test-need-help>"
    assert len(draft["body"]) > 0


@pytest.mark.asyncio
async def test_need_help_no_active_task_redirects() -> None:
    """NEED_HELP with no active task redirects to get a task first."""
    with patch("app.graph.nodes.need_help._ENABLE_LANGGRAPH_PATH", True):
        from app.graph.nodes.need_help import need_help_node

        state: State = {
            "peer": "<test-need-help-no-task>",
            "incoming": "I need help",
            "intent": "NEED_HELP",
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

        result = await need_help_node(state)

    assert result["pending_outbound"]
    body = result["pending_outbound"][0]["body"]
    # Should redirect to getting a task
    assert "task" in body.lower() or "time" in body.lower()


def test_need_help_prompt_parity() -> None:
    """need_help.md.j2 must contain all required sections."""
    from app.prompts.loader import render_with_defaults

    rendered = render_with_defaults(
        "need_help.md.j2",
        {},
        defaults={
            "task_title": "placeholder",
            "inline_steps": "1. Step\n2. Step",
            "user_message": "",
        },
    )

    for section in ["Breakdown Assistance", "Shame Prevention"]:
        assert section in rendered, f"Missing section: {section}"


# ---------------------------------------------------------------------------
# CHECK_IN node tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_in_node_sends_friendly_message() -> None:
    """CHECK_IN node sends a casual, non-supervisory check-in."""
    check_in_response = json.dumps({
        "check_in_message": "How's the placeholder task going? Still at it?",
    })

    active = _active_task("Placeholder in-progress task")
    active["started_at"] = (datetime.now(UTC) - timedelta(minutes=60)).isoformat()
    active["check_in_count"] = 0

    with (
        patch("app.graph.nodes.check_in._ENABLE_LANGGRAPH_PATH", True),
        patch("app.models.llm", return_value=_mock_llm_response(check_in_response)),
    ):
        from app.graph.nodes.check_in import check_in_node

        state: State = {
            "peer": "<test-check-in>",
            "incoming": "",  # System-triggered, no user message
            "intent": "CHECK_IN",
            "messages": [],
            "active_task": active,
            "streak": 0,
            "tasks_completed_today": 0,
            "user_prefs": {},
            "mood": None,
            "available_minutes": None,
            "conversation_state": "active",
            "pending_outbound": [],
        }

        result = await check_in_node(state)

    assert result["pending_outbound"]
    draft = result["pending_outbound"][0]
    assert draft["recipient"] == "<test-check-in>"
    assert len(draft["body"]) > 0
    assert result.get("conversation_state") == "checking_in"
    # Check-in count incremented
    assert result.get("active_task", {}).get("check_in_count", 0) == 1


@pytest.mark.asyncio
async def test_check_in_node_skips_when_no_active_task() -> None:
    """CHECK_IN node exits cleanly when no active task."""
    with patch("app.graph.nodes.check_in._ENABLE_LANGGRAPH_PATH", True):
        from app.graph.nodes.check_in import check_in_node

        state: State = {
            "peer": "<test-check-in-skip>",
            "incoming": "",
            "intent": "CHECK_IN",
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

        result = await check_in_node(state)

    # No outbound message when no active task
    assert not result.get("pending_outbound", [])
    assert result.get("conversation_state") == "idle"


def test_check_in_prompt_parity() -> None:
    """check_in.md.j2 must contain all required sections."""
    from app.prompts.loader import render_with_defaults

    rendered = render_with_defaults(
        "check_in.md.j2",
        {},
        defaults={
            "task_title": "placeholder",
            "time_estimate": 30,
            "elapsed_minutes": 45,
            "check_in_count": 0,
        },
    )

    for section in ["Check-In Handling", "Shame Prevention"]:
        assert section in rendered, f"Missing section: {section}"


# ---------------------------------------------------------------------------
# COMPLETE node tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_node_marks_task_done_and_rewards() -> None:
    """COMPLETE node marks task done and returns a reward message."""
    page_id = str(uuid.uuid4())
    active = _active_task("Placeholder completed task", page_id=page_id)

    with (
        patch("app.graph.nodes.complete._ENABLE_LANGGRAPH_PATH", True),
        patch("app.tools.notion.update_status", new_callable=AsyncMock),
        patch("app.tools.rewards.maybe_reward", new_callable=AsyncMock, return_value="Nice work! ✨"),
    ):
        from app.graph.nodes.complete import complete_node

        state: State = {
            "peer": "<test-complete>",
            "incoming": "Done!",
            "intent": "COMPLETE",
            "messages": [],
            "active_task": active,
            "streak": 2,
            "tasks_completed_today": 2,
            "user_prefs": {},
            "mood": None,
            "available_minutes": None,
            "conversation_state": "active",
            "pending_outbound": [],
        }

        result = await complete_node(state)

    assert result["pending_outbound"]
    draft = result["pending_outbound"][0]
    assert draft["recipient"] == "<test-complete>"
    assert "✨" in draft["body"] or "work" in draft["body"].lower() or "nice" in draft["body"].lower()

    # State updated
    assert result.get("active_task") is None
    assert result.get("streak", 0) == 3  # streak + 1
    assert result.get("conversation_state") == "idle"


@pytest.mark.asyncio
async def test_complete_node_no_active_task_still_confirms() -> None:
    """COMPLETE with no active task (reminder completion) still sends confirmation."""
    with (
        patch("app.graph.nodes.complete._ENABLE_LANGGRAPH_PATH", True),
        patch("app.tools.notion.update_status", new_callable=AsyncMock),
    ):
        from app.graph.nodes.complete import complete_node

        state: State = {
            "peer": "<test-complete-no-task>",
            "incoming": "I did it",
            "intent": "COMPLETE",
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

        result = await complete_node(state)

    assert result["pending_outbound"]
    body = result["pending_outbound"][0]["body"]
    assert len(body) > 0


@pytest.mark.asyncio
async def test_complete_node_dormant_when_flag_off() -> None:
    """COMPLETE node returns stub when ENABLE_LANGGRAPH_PATH=false."""
    from app.graph.nodes.complete import complete_node

    state: State = {
        "peer": "<test-complete-dormant>",
        "incoming": "done",
        "intent": "COMPLETE",
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

    result = await complete_node(state)
    assert result["pending_outbound"]
    assert "ENABLE_LANGGRAPH_PATH=false" in result["pending_outbound"][0]["body"] or \
           "stub" in result["pending_outbound"][0]["body"].lower()


# ---------------------------------------------------------------------------
# CHECK_IN APScheduler job tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_in_dispatcher_job_registered() -> None:
    """check_in_dispatcher job must exist in SCHEDULED_JOBS."""
    from app.scheduler.jobs import SCHEDULED_JOBS

    job_ids = {j.id for j in SCHEDULED_JOBS}
    assert "check_in_dispatcher" in job_ids, (
        "check_in_dispatcher job not found in SCHEDULED_JOBS. "
        "PR-B4 requires this job for autonomous check-ins."
    )


def test_check_in_dispatcher_runs_on_interval() -> None:
    """check_in_dispatcher must use an IntervalTrigger (not CronTrigger)."""
    from apscheduler.triggers.interval import IntervalTrigger

    from app.scheduler.jobs import SCHEDULED_JOBS

    job = next((j for j in SCHEDULED_JOBS if j.id == "check_in_dispatcher"), None)
    assert job is not None, "check_in_dispatcher not found"
    assert isinstance(job.trigger, IntervalTrigger), (
        "check_in_dispatcher must use IntervalTrigger (fires every N minutes)"
    )
