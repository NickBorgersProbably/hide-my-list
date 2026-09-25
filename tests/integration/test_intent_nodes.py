"""Integration tests for remaining 5 intent nodes: REJECT, CANNOT_FINISH, NEED_HELP, CHECK_IN, COMPLETE.

All use mocked Notion and mocked LLM. No real network calls.

Covers:
- Each node's happy path
- Section-anchor parity for each node's prompt template
- CHECK_IN APScheduler job integration
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

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
        "selected_at": datetime.now(UTC).isoformat(),
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


def test_rejection_prompt_parity() -> None:
    """rejection.md.j2 must contain all required sections from source doc."""
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
        patch("app.tools.notion.update_status", new_callable=AsyncMock),
        patch(
            "app.tools.rewards.maybe_reward",
            new_callable=AsyncMock,
            return_value={"text": "Nice work! ✨", "attachment_path": None},
        ),
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
    with patch("app.tools.notion.update_status", new_callable=AsyncMock):
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


# ---------------------------------------------------------------------------
# COMPLETE node: resolving the task named in the message
# ---------------------------------------------------------------------------

def _notion_task_page(page_id: str, title: str, status: str = "Pending") -> dict:
    return {
        "id": page_id,
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Status": {"select": {"name": status}},
            "Is Reminder": {"checkbox": False},
        },
    }


def _complete_state(**overrides: Any) -> State:
    state: dict[str, Any] = {
        "peer": "<test-complete-named>",
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
    state.update(overrides)
    return state  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_complete_node_resolves_task_named_in_the_message() -> None:
    """The production failure: no active task, no live reminder, task named in text."""
    from app.graph.nodes import complete as complete_module

    update_status = AsyncMock()
    reward_mock = AsyncMock(return_value={"text": "Nice work!", "attachment_path": None})
    query_all = AsyncMock(return_value={"results": [
        _notion_task_page("<page_A>", "Wash the dishes"),
        _notion_task_page("<page_B>", "Email the landlord"),
    ]})

    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", query_all),
        patch("app.tools.rewards.maybe_reward", reward_mock),
        patch.object(complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)),
        patch(
            "app.models.llm",
            return_value=_mock_llm_response(
                json.dumps({"matched_page_id": "<page_A>", "confidence": 0.95})
            ),
        ),
    ):
        result = await complete_module.complete_node(
            _complete_state(incoming="finally washed the dishes")
        )

    update_status.assert_awaited_once_with(page_id="<page_A>", new_status="Completed")
    assert reward_mock.await_args.kwargs["notion_page_id"] == "<page_A>"
    assert reward_mock.await_args.kwargs["task_title"] == "Wash the dishes"
    assert result["pending_outbound"][0]["notion_page_id"] == "<page_A>"
    assert result["streak"] == 1


@pytest.mark.asyncio
async def test_complete_node_named_task_outranks_a_different_active_task() -> None:
    """Naming task B while task A is selected must not complete A."""
    from app.graph.nodes import complete as complete_module

    update_status = AsyncMock()
    reward_mock = AsyncMock(return_value={"text": "Nice work!", "attachment_path": None})
    query_all = AsyncMock(return_value={"results": [
        _notion_task_page("<page_B>", "Wash the dishes"),
    ]})

    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", query_all),
        patch("app.tools.rewards.maybe_reward", reward_mock),
        patch.object(complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)),
        patch(
            "app.models.llm",
            return_value=_mock_llm_response(
                json.dumps({"matched_page_id": "<page_B>", "confidence": 0.95})
            ),
        ),
    ):
        result = await complete_module.complete_node(
            _complete_state(
                incoming="done with the dishes",
                active_task=_active_task("Fold the laundry", page_id="<page_A>"),
            )
        )

    update_status.assert_awaited_once_with(page_id="<page_B>", new_status="Completed")
    assert result["pending_outbound"][0]["notion_page_id"] == "<page_B>"


@pytest.mark.asyncio
async def test_complete_node_below_threshold_clarifies_rather_than_writing() -> None:
    """A sub-threshold match with candidates must clarify, not fall back to active task.

    "done with the dishes" shortlists "Wash the dishes" (page_B) at 0.85 — the
    model found a candidate but was not confident enough. Completing the active
    task "Fold the laundry" (page_A) would be wrong: the message named dishes,
    not laundry. The candidate-rejection guard catches this and clarifies.
    """
    from app.graph.nodes import complete as complete_module

    update_status = AsyncMock()
    reward_mock = AsyncMock(return_value={"text": "Nice work!", "attachment_path": None})
    query_all = AsyncMock(return_value={"results": [
        _notion_task_page("<page_B>", "Wash the dishes"),
    ]})

    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", query_all),
        patch("app.tools.rewards.maybe_reward", reward_mock),
        patch.object(complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)),
        patch(
            "app.models.llm",
            return_value=_mock_llm_response(
                json.dumps({"matched_page_id": "<page_B>", "confidence": 0.85})
            ),
        ),
    ):
        result = await complete_module.complete_node(
            _complete_state(
                incoming="done with the dishes",
                active_task=_active_task("Fold the laundry", page_id="<page_A>"),
            )
        )

    update_status.assert_not_awaited()
    reward_mock.assert_not_awaited()
    assert result["pending_outbound"][0]["notion_page_id"] is None


@pytest.mark.asyncio
async def test_complete_node_ignores_a_task_the_user_still_has_to_do() -> None:
    """"done, now I need to call mom" lexically shortlists "Call mom".

    Only the model sees the whole sentence, so a null match has to leave the
    task open rather than falling through to some other resolution.
    """
    from app.graph.nodes import complete as complete_module

    update_status = AsyncMock()
    reward_mock = AsyncMock(return_value={"text": "Nice work!", "attachment_path": None})
    query_all = AsyncMock(return_value={"results": [
        _notion_task_page("<page_A>", "Call mom"),
    ]})

    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", query_all),
        patch("app.tools.rewards.maybe_reward", reward_mock),
        patch.object(complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)),
        patch(
            "app.models.llm",
            return_value=_mock_llm_response(
                json.dumps({"matched_page_id": None, "confidence": 0.0})
            ),
        ),
    ):
        result = await complete_module.complete_node(
            _complete_state(incoming="done, now I need to call mom")
        )

    update_status.assert_not_awaited()
    reward_mock.assert_not_awaited()
    assert result["pending_outbound"][0]["notion_page_id"] is None


@pytest.mark.asyncio
async def test_complete_node_survives_a_notion_failure_during_matching() -> None:
    """The lookup is additive: when it fails, context-based resolution still runs."""
    from app.graph.nodes import complete as complete_module

    update_status = AsyncMock()
    reward_mock = AsyncMock(return_value={"text": "Nice work!", "attachment_path": None})

    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", AsyncMock(side_effect=RuntimeError("Notion down"))),
        patch("app.tools.rewards.maybe_reward", reward_mock),
        patch.object(complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)),
    ):
        result = await complete_module.complete_node(
            _complete_state(
                incoming="done with the dishes",
                active_task=_active_task("Wash the dishes", page_id="<page_A>"),
            )
        )

    update_status.assert_awaited_once_with(page_id="<page_A>", new_status="Completed")
    assert result["pending_outbound"][0]["notion_page_id"] == "<page_A>"


@pytest.mark.asyncio
async def test_complete_node_skips_the_lookup_for_a_bare_completion() -> None:
    """"done!" costs no Notion read and no model call."""
    from app.graph.nodes import complete as complete_module

    query_all = AsyncMock()
    llm_factory = MagicMock()

    with (
        patch("app.tools.notion.update_status", new_callable=AsyncMock),
        patch("app.tools.notion.query_all", query_all),
        patch(
            "app.tools.rewards.maybe_reward",
            new_callable=AsyncMock,
            return_value={"text": "Nice work!", "attachment_path": None},
        ),
        patch.object(complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)),
        patch("app.models.llm", llm_factory),
    ):
        await complete_module.complete_node(
            _complete_state(
                incoming="done!",
                active_task=_active_task("Wash the dishes", page_id="<page_A>"),
            )
        )

    query_all.assert_not_awaited()
    llm_factory.assert_not_called()


@pytest.mark.asyncio
async def test_complete_node_keeps_active_task_metadata_when_the_name_matches_it() -> None:
    """Naming the task already in hand still runs the lookup, and keeps the metadata.

    There is no lexical shortcut past the model: overlapping a task's words is
    not the same as saying it is finished, so the lookup runs whenever the
    message names anything. When it lands on the page state already holds, the
    arbitration keeps the active-task target — it is the only source carrying
    work_type and energy_required, and a title-match target would silently
    hand the reward call two empty strings.
    """
    from app.graph.nodes import complete as complete_module

    query_all = AsyncMock(return_value={"results": [
        _notion_task_page("<page_A>", "Fold the laundry"),
    ]})
    reward_mock = AsyncMock(return_value={"text": "Nice work!", "attachment_path": None})

    with (
        patch("app.tools.notion.update_status", new_callable=AsyncMock),
        patch("app.tools.notion.query_all", query_all),
        patch("app.tools.rewards.maybe_reward", reward_mock),
        patch.object(complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)),
        patch("app.models.llm", return_value=_mock_llm_response(
            json.dumps({"matched_page_id": "<page_A>", "confidence": 0.95})
        )),
    ):
        await complete_module.complete_node(
            _complete_state(
                incoming="done with the laundry",
                active_task=_active_task("Fold the laundry", page_id="<page_A>"),
            )
        )

    query_all.assert_awaited()
    assert reward_mock.await_args.kwargs["notion_page_id"] == "<page_A>"
    # active_task is the only source carrying reward metadata; it must survive.
    assert reward_mock.await_args.kwargs["work_type"] == "focus"
    assert reward_mock.await_args.kwargs["energy_required"] == "Medium"


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


# ---------------------------------------------------------------------------
# Recent-task ledger and turn_actions deltas
#
# Every writer returns the full new `recent_tasks` list (plain replace, no
# reducer) and the `turn_actions` it performed this turn. These tests pin the
# delta each node writes, so a node that stops recording what it did — the
# reason a bare "done" or "what task?" had nothing to anchor to — fails here.
# ---------------------------------------------------------------------------


def _pending_page(page_id: str, title: str, *, minutes: int = 20) -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Status": {"select": {"name": "Pending"}},
            "Work Type": {"select": {"name": "Independent"}},
            "Energy Required": {"select": {"name": "Low"}},
            "Urgency": {"number": 50},
            "Time Estimate (min)": {"number": minutes},
            "Rejection Count": {"number": 0},
        },
    }


def _ledger_state(**overrides: Any) -> State:
    state: dict[str, Any] = {
        "peer": "<test-ledger>",
        "incoming": "",
        "intent": None,
        "messages": [],
        "active_task": None,
        "streak": 0,
        "tasks_completed_today": 0,
        "user_prefs": {},
        "mood": None,
        "available_minutes": None,
        "conversation_state": "idle",
        "pending_outbound": [],
        "recent_tasks": [],
        "turn_actions": [],
    }
    state.update(overrides)
    return state  # type: ignore[return-value]


def _ledger_view(ledger: list[dict[str, Any]]) -> list[tuple[str, str, str, str]]:
    """Ledger entries without the timestamp, which the node stamps itself."""
    for entry in ledger:
        assert isinstance(entry["at"], str) and entry["at"], "every entry carries an ISO timestamp"
    return [(e["page_id"], e["title"], e["kind"], e["event"]) for e in ledger]


@pytest.mark.asyncio
async def test_selection_node_records_suggested_task() -> None:
    import inspect

    from app.tools import notion

    update_status = AsyncMock()
    response = json.dumps({
        "selected_task_id": "<page_A>",
        "score": 0.9,
        "reasoning": "fits",
        "user_message": "How about {task}?",
    })
    with (
        patch("app.tools.notion.query_pending", AsyncMock(return_value={"results": [
            _pending_page("<page_A>", "Water the plants"),
            _pending_page("<page_B>", "Sort the mail"),
        ]})),
        patch("app.tools.notion.update_status", update_status),
        patch("app.models.llm", return_value=_mock_llm_response(response)),
    ):
        from app.graph.nodes.selection import selection_node

        result = await selection_node(_ledger_state(incoming="what should I do?", intent="GET_TASK"))

    update_status.assert_awaited_once()
    call = update_status.await_args
    bound = inspect.signature(notion.update_status).bind(*call.args, **call.kwargs)
    assert bound.arguments == {"page_id": "<page_A>", "new_status": "In Progress"}

    assert result["active_task"]["page_id"] == "<page_A>"
    assert result["active_task"]["title"] == "Water the plants"
    assert _ledger_view(result["recent_tasks"]) == [
        ("<page_A>", "Water the plants", "task", "suggested"),
    ]
    assert result["turn_actions"] == [
        {"action": "notion.update_status", "page_id": "<page_A>"},
        {"action": "suggest", "page_id": "<page_A>"},
    ]


@pytest.mark.asyncio
async def test_selection_node_unknown_page_id_is_not_suggested() -> None:
    """A selected id outside the scored list is no selection at all.

    The model named a page the node never offered it. Treating that as a
    suggestion wrote In Progress to an unknown page and built an ActiveTask with
    an empty title, so the user was offered "this focus task" with no name.
    """
    update_status = AsyncMock()
    response = json.dumps({
        "selected_task_id": "<page_unknown>",
        "score": 0.9,
        "reasoning": "fits",
        "user_message": "How about this focus task?",
    })
    with (
        patch("app.tools.notion.query_pending", AsyncMock(return_value={"results": [
            _pending_page("<page_A>", "Water the plants"),
        ]})),
        patch("app.tools.notion.update_status", update_status),
        patch("app.models.llm", return_value=_mock_llm_response(response)),
        capture_logs() as logs,
    ):
        from app.graph.nodes.selection import selection_node

        result = await selection_node(_ledger_state(incoming="anything I can knock out?"))

    update_status.assert_not_awaited()
    assert result["active_task"] is None
    draft = result["pending_outbound"][0]
    assert draft["notion_page_id"] is None
    assert "notion_page_title" not in draft
    assert "focus task" not in draft["body"]
    assert "Nothing quite fits" in draft["body"]
    assert result["recent_tasks"] == []
    assert result["turn_actions"] == []
    unknown = [e for e in logs if e.get("event") == "selection_node.unknown_page_id"]
    assert len(unknown) == 1
    assert unknown[0]["notion_page_id"] == "<page_unknown>"
    assert "selection_node.error" not in {e.get("event") for e in logs}


@pytest.mark.asyncio
async def test_selection_node_blank_title_is_not_suggested() -> None:
    """A page that is in the list but has no title cannot be named, so it is not offered."""
    update_status = AsyncMock()
    response = json.dumps({
        "selected_task_id": "<page_blank>",
        "score": 0.9,
        "reasoning": "fits",
        "user_message": "How about {task}?",
    })
    with (
        patch("app.tools.notion.query_pending", AsyncMock(return_value={"results": [
            _pending_page("<page_blank>", "   "),
        ]})),
        patch("app.tools.notion.update_status", update_status),
        patch("app.models.llm", return_value=_mock_llm_response(response)),
    ):
        from app.graph.nodes.selection import selection_node

        result = await selection_node(_ledger_state(incoming="what now?"))

    update_status.assert_not_awaited()
    assert result["active_task"] is None
    assert "{task}" not in result["pending_outbound"][0]["body"]
    assert result["pending_outbound"][0]["notion_page_id"] is None


@pytest.mark.asyncio
async def test_selection_prompt_carries_the_user_message_and_history() -> None:
    """The model reads available time from the message when state has none."""
    from langchain_core.messages import AIMessage, HumanMessage

    model = _mock_llm_response(json.dumps({
        "selected_task_id": None,
        "score": 0.0,
        "reasoning": "",
        "user_message": "Nothing quite fits right now.",
    }))
    with (
        patch("app.tools.notion.query_pending", AsyncMock(return_value={"results": []})),
        patch("app.models.llm", return_value=model),
    ):
        from app.graph.nodes.selection import selection_node

        await selection_node(_ledger_state(
            incoming="I have 2 hours and feel sharp",
            messages=[HumanMessage(content="morning"), AIMessage(content="Morning!")],
        ))

    system_prompt = model.ainvoke.await_args.args[0][0].content
    assert "I have 2 hours and feel sharp" in system_prompt
    assert "assistant: Morning!" in system_prompt
    assert "not stated" in system_prompt


@pytest.mark.asyncio
async def test_rejection_node_records_rejected_and_suggested() -> None:
    update_property = AsyncMock()
    response = json.dumps({
        "rejection_category": "mood_mismatch",
        "alternative_task_id": "<page_B>",
        "user_message": "Fair — how about {task} instead?",
    })
    active = _active_task("Water the plants", page_id="<page_A>")
    with (
        patch("app.tools.notion.query_pending", AsyncMock(return_value={"results": [
            _pending_page("<page_A>", "Water the plants"),
            _pending_page("<page_B>", "Sort the mail"),
        ]})),
        patch("app.tools.notion.update_property", update_property),
        patch("app.models.llm", return_value=_mock_llm_response(response)),
    ):
        from app.graph.nodes.rejection import rejection_node

        result = await rejection_node(_ledger_state(incoming="not that one", active_task=active))

    assert _ledger_view(result["recent_tasks"]) == [
        ("<page_B>", "Sort the mail", "task", "suggested"),
        ("<page_A>", "Water the plants", "task", "rejected"),
    ]
    assert result["turn_actions"] == [
        {"action": "notion.update_property", "page_id": "<page_A>"},
        {"action": "suggest", "page_id": "<page_B>"},
    ]


@pytest.mark.asyncio
async def test_rejection_node_unknown_alternative_is_not_recorded() -> None:
    response = json.dumps({
        "alternative_task_id": "<page_unknown>",
        "user_message": "Want something else?",
    })
    active = _active_task("Water the plants", page_id="<page_A>")
    with (
        patch("app.tools.notion.query_pending", AsyncMock(return_value={"results": []})),
        patch("app.tools.notion.update_property", AsyncMock()),
        patch("app.models.llm", return_value=_mock_llm_response(response)),
    ):
        from app.graph.nodes.rejection import rejection_node

        result = await rejection_node(_ledger_state(incoming="nah", active_task=active))

    assert _ledger_view(result["recent_tasks"]) == [
        ("<page_A>", "Water the plants", "task", "rejected"),
    ]
    assert {"action": "suggest", "page_id": "<page_unknown>"} not in result["turn_actions"]


def _intake_response(
    *, title: str, is_reminder: bool = False, remind_at: str | None = None
) -> str:
    return json.dumps({
        "action": "save",
        "title": title,
        "work_type": "independent",
        "urgency": 50,
        "time_estimate_minutes": 10,
        "energy_required": "Low",
        "is_reminder": is_reminder,
        "remind_at": remind_at,
        "due_at": None,
        "use_hidden_subtasks": False,
        "sub_tasks": [],
        "inline_steps": "",
        "confirmation_message": "Got it — {task}.",
    })


@pytest.mark.asyncio
async def test_intake_node_records_added_task() -> None:
    create_task = AsyncMock(return_value={"id": "<page_new>"})
    with (
        patch("app.tools.notion.query_all", AsyncMock(return_value={"results": []})),
        patch("app.tools.notion.create_task", create_task),
        patch(
            "app.models.llm",
            return_value=_mock_llm_response(_intake_response(title="Sort the mail")),
        ),
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(_ledger_state(incoming="I need to sort the mail"))

    create_task.assert_awaited_once()
    assert _ledger_view(result["recent_tasks"]) == [
        ("<page_new>", "Sort the mail", "task", "added"),
    ]
    assert result["turn_actions"] == [{"action": "notion.create_task", "page_id": "<page_new>"}]


@pytest.mark.asyncio
async def test_intake_node_records_added_reminder() -> None:
    create_reminder = AsyncMock(return_value={"id": "<page_rem>"})
    conn_ctx = AsyncMock()
    conn_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
    conn_ctx.__aexit__ = AsyncMock(return_value=None)
    with (
        patch("app.tools.notion.create_reminder", create_reminder),
        patch("app.tools.db.get_db_conn", return_value=conn_ctx),
        patch("app.tools.reminders.enqueue", AsyncMock(return_value=uuid.uuid4())),
        patch("app.models.llm", return_value=_mock_llm_response(_intake_response(
            title="Take the bins out",
            is_reminder=True,
            remind_at="2026-01-02T20:00:00-06:00",
        ))),
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(
            _ledger_state(incoming="remind me to take the bins out at 8pm")
        )

    create_reminder.assert_awaited_once()
    assert _ledger_view(result["recent_tasks"]) == [
        ("<page_rem>", "Take the bins out", "reminder", "added"),
    ]
    assert result["turn_actions"] == [
        {"action": "notion.create_reminder", "page_id": "<page_rem>"}
    ]


@pytest.mark.asyncio
async def test_intake_node_clarify_records_the_question_only() -> None:
    existing = [{
        "page_id": "<page_old>",
        "title": "Sort the mail",
        "kind": "task",
        "event": "added",
        "at": datetime.now(UTC).isoformat(),
    }]
    response = json.dumps({"action": "clarify", "clarification_question": "Which one?"})
    with patch("app.models.llm", return_value=_mock_llm_response(response)):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(
            _ledger_state(incoming="add that thing", recent_tasks=existing)
        )

    assert result["turn_actions"] == [{"action": "clarify", "page_id": None}]
    assert result.get("recent_tasks", existing) == existing


@pytest.mark.asyncio
async def test_complete_node_records_completed_task() -> None:
    from app.graph.nodes import complete as complete_module

    active = _active_task("Water the plants", page_id="<page_A>")
    with (
        patch("app.tools.notion.update_status", new_callable=AsyncMock),
        patch(
            "app.tools.rewards.maybe_reward",
            new_callable=AsyncMock,
            return_value={"text": "Nice work!", "attachment_path": None},
        ),
        patch.object(complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)),
    ):
        result = await complete_module.complete_node(
            _ledger_state(incoming="done!", intent="COMPLETE", active_task=active)
        )

    assert _ledger_view(result["recent_tasks"]) == [
        ("<page_A>", "Water the plants", "task", "completed"),
    ]
    assert result["turn_actions"] == [
        {"action": "notion.update_status", "page_id": "<page_A>"},
        {"action": "reward", "page_id": "<page_A>"},
    ]


@pytest.mark.asyncio
async def test_complete_node_from_a_delivered_reminder_records_no_body_as_title() -> None:
    """A recent_outbound target's title is the sent reminder body; the ledger never stores it."""
    from app.graph.nodes import complete as complete_module

    recent_target = complete_module._CompletionTarget(
        source="recent_outbound",
        page_id="<page_R>",
        task_title="Reminder body placeholder",
        work_type="",
        energy_required="",
        context_at=datetime.now(UTC),
        signal_timestamp=1,
    )
    known = [{
        "page_id": "<page_R>",
        "title": "Take the bins out",
        "kind": "reminder",
        "event": "reminded",
        "at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
    }]
    with (
        patch("app.tools.notion.update_status", new_callable=AsyncMock) as update_status,
        patch(
            "app.tools.rewards.maybe_reward",
            new_callable=AsyncMock,
            return_value={"text": "Nice work!", "attachment_path": None},
        ),
        patch.object(
            complete_module, "_load_recent_outbound_target", AsyncMock(return_value=recent_target)
        ),
        patch.object(complete_module, "_clear_recent_outbound", AsyncMock()),
    ):
        result = await complete_module.complete_node(
            _ledger_state(incoming="done", intent="COMPLETE", recent_tasks=known)
        )

    update_status.assert_not_awaited()
    assert _ledger_view(result["recent_tasks"]) == [
        ("<page_R>", "Take the bins out", "reminder", "completed"),
    ]
    assert result["turn_actions"] == [{"action": "reward", "page_id": "<page_R>"}]


@pytest.mark.asyncio
async def test_complete_node_clarify_records_the_question() -> None:
    from app.graph.nodes import complete as complete_module

    with (
        patch("app.tools.notion.update_status", new_callable=AsyncMock) as update_status,
        patch.object(complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)),
    ):
        result = await complete_module.complete_node(
            _ledger_state(incoming="done!", intent="COMPLETE")
        )

    update_status.assert_not_awaited()
    assert result["turn_actions"] == [{"action": "clarify", "page_id": None}]
    assert result.get("recent_tasks", []) == []
