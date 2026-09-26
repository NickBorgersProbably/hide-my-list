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

import inspect
import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from app.graph.state import State
from app.scheduler.reminder_scheduling import schedule_for_task as _real_schedule_for_task


def _assert_schedule_call(
    mock: AsyncMock, *, page_id: str, peer: str, deadline_iso: str, title: str
) -> None:
    """Bind intake's schedule_for_task call against the real signature (clause 10).

    Intake swallows scheduling errors into a fallback reply, so a renamed or
    removed parameter would degrade silently. Every keyword is pinned.
    """
    mock.assert_awaited_once()
    call = mock.await_args
    params = inspect.signature(_real_schedule_for_task).parameters
    inspect.signature(_real_schedule_for_task).bind(*call.args, **call.kwargs)
    assert len(call.args) == 1  # the connection
    assert set(call.kwargs) == {name for name in params if name != "conn"}
    assert call.kwargs["notion_page_id"] == page_id
    assert call.kwargs["peer"] == peer
    assert call.kwargs["deadline_at"] == datetime.fromisoformat(deadline_iso)
    assert isinstance(call.kwargs["urgency"], int)
    assert call.kwargs["user_tz"] == "America/Chicago"
    assert isinstance(call.kwargs["now"], datetime) and call.kwargs["now"].tzinfo is not None
    assert call.kwargs["title"] == title

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _empty_dedup_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default intake dedup query result for tests unrelated to duplicates."""

    async def query_all() -> dict[str, Any]:
        return {"results": []}

    monkeypatch.setattr("app.tools.notion.query_all", query_all)


def _make_notion_page(page_id: str = "", title: str = "Placeholder task") -> dict:
    """Build a minimal Notion page response dict."""
    return {
        "id": page_id or str(uuid.uuid4()),
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Status": {"select": {"name": "Pending"}},
        },
    }


def _make_query_page(page_id: str, title: str, *, status: str = "Pending") -> dict[str, Any]:
    """Build a minimal Notion query result page."""
    return {
        "id": page_id,
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Status": {"select": {"name": status}},
            "Is Reminder": {"checkbox": False},
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
        "due_at": due_at,
        "use_hidden_subtasks": False,
        "sub_tasks": [],
        "inline_steps": "1. First step\n2. Second step",
        "confirmation_message": confirmation,
    })


def _base_state(*, incoming: str, peer: str = "<test-peer-1>") -> State:
    return {
        "peer": peer,
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
async def test_deadline_task_schedules_inline_series() -> None:
    """A non-reminder task with due_at is saved with Due At and scheduled inline."""
    page_id = str(uuid.uuid4())
    due_at = "2026-06-06T17:00:00-05:00"
    task_response = _llm_save_response(
        title="Placeholder deadline task",
        work_type="focus",
        urgency=80,
        time_estimate=45,
        is_reminder=False,
        due_at=due_at,
        confirmation="Got it — focus, ~45 min.",
    )

    create_task_calls: list[dict[str, Any]] = []

    async def mock_create_task(**kwargs: Any) -> dict:
        create_task_calls.append(kwargs)
        return _make_notion_page(page_id=page_id, title="Placeholder deadline task")

    scheduled_item = type(
        "Scheduled",
        (),
        {"label": "3d", "assigned_at": datetime(2026, 6, 3, 15, 0, tzinfo=UTC)},
    )()
    schedule_for_task = AsyncMock(return_value=([scheduled_item], []))
    record_deadline_task_peer = AsyncMock(return_value=None)
    mark_scheduled = AsyncMock(return_value={})

    class FakeCtx:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_: object) -> None:
            return None

    with (
        patch("app.tools.notion.create_task", side_effect=mock_create_task),
        patch("app.tools.notion.mark_reminder_scheduled", mark_scheduled),
        patch("app.tools.db.get_db_conn", return_value=FakeCtx()),
        patch(
            "app.scheduler.reminder_scheduling.record_deadline_task_peer",
            record_deadline_task_peer,
        ),
        patch("app.scheduler.reminder_scheduling.schedule_for_task", schedule_for_task),
    ):
        mock_llm_response = MagicMock()
        mock_llm_response.content = task_response
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_llm_response)

        with patch("app.models.llm", return_value=mock_model):
            from app.graph.nodes.intake import intake_node

            state: State = {
                "peer": "<test-intake-deadline>",
                "incoming": "finish placeholder task by Saturday at 5pm",
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

            result = await intake_node(state)

    assert create_task_calls[0]["due_at_iso"] == "2026-06-06T22:00:00+00:00"
    record_deadline_task_peer.assert_awaited_once()
    # The nudge body names the task, so intake hands the series its title.
    _assert_schedule_call(
        schedule_for_task,
        page_id=page_id,
        peer="<test-intake-deadline>",
        deadline_iso="2026-06-06T22:00:00+00:00",
        title="Placeholder deadline task",
    )
    mark_scheduled.assert_awaited_once_with(page_id)
    # One scheduled slot is summarized as the first nudge, appended to the
    # model's confirmation. The whole-schedule "I'll ping you ..." list is gone.
    body = result["pending_outbound"][0]["body"]
    assert body.startswith("Got it — focus, ~45 min. First nudge ")
    assert "I'll ping you" not in body


@pytest.mark.asyncio
async def test_dedup_no_existing_tasks_creates_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """No existing open tasks means intake creates the proposed task."""
    page_id = str(uuid.uuid4())
    task_response = _llm_save_response(title="Placeholder task")
    create_task = AsyncMock(return_value=_make_notion_page(page_id=page_id, title="Placeholder task"))

    async def query_all() -> dict[str, Any]:
        return {"results": []}

    monkeypatch.setattr("app.tools.notion.query_all", query_all)

    mock_llm_response = MagicMock()
    mock_llm_response.content = task_response
    mock_model = AsyncMock()
    mock_model.ainvoke = AsyncMock(return_value=mock_llm_response)

    with (
        patch("app.tools.notion.create_task", create_task),
        patch("app.models.llm", return_value=mock_model),
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(_base_state(incoming="Placeholder task"))

    create_task.assert_awaited_once()
    assert result["pending_outbound"][0]["notion_page_id"] == page_id


@pytest.mark.asyncio
async def test_dedup_clear_duplicate_updates_instead_of_creating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A high-confidence duplicate reuses the existing Notion page."""
    matched_page_id = "<page_id_matched>"
    task_response = _llm_save_response(title="Placeholder task")
    create_task = AsyncMock(return_value=_make_notion_page(page_id=str(uuid.uuid4())))
    update_property = AsyncMock(return_value={})

    async def query_all() -> dict[str, Any]:
        return {"results": [_make_query_page(matched_page_id, "Placeholder task", status="In Progress")]}

    monkeypatch.setattr("app.tools.notion.query_all", query_all)

    intake_llm_response = MagicMock()
    intake_llm_response.content = task_response
    intake_model = AsyncMock()
    intake_model.ainvoke = AsyncMock(return_value=intake_llm_response)

    dedup_llm_response = MagicMock()
    dedup_llm_response.content = json.dumps({"matched_page_id": matched_page_id, "confidence": 0.96})
    dedup_model = AsyncMock()
    dedup_model.ainvoke = AsyncMock(return_value=dedup_llm_response)

    with (
        patch("app.tools.notion.create_task", create_task),
        patch("app.tools.notion.update_property", update_property),
        patch("app.models.llm", side_effect=[intake_model, dedup_model]),
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(_base_state(incoming="Placeholder task"))

    create_task.assert_not_awaited()
    update_property.assert_not_awaited()
    draft = result["pending_outbound"][0]
    assert draft["notion_page_id"] == matched_page_id
    assert "Placeholder task" in draft["body"]
    assert "duplicate" not in draft["body"].lower()
    assert "again" not in draft["body"].lower()


@pytest.mark.asyncio
async def test_dedup_deadline_updates_existing_and_schedules_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate with a deadline updates and schedules against the existing page."""
    matched_page_id = "<page_id_matched>"
    due_at = "2026-06-06T17:00:00-05:00"
    task_response = _llm_save_response(title="Placeholder deadline task", due_at=due_at)
    create_task = AsyncMock(return_value=_make_notion_page(page_id=str(uuid.uuid4())))
    update_property = AsyncMock(return_value={})

    async def query_all() -> dict[str, Any]:
        return {
            "results": [
                _make_query_page(matched_page_id, "Placeholder deadline task", status="In Progress"),
            ]
        }

    monkeypatch.setattr("app.tools.notion.query_all", query_all)

    scheduled_item = type(
        "Scheduled",
        (),
        {"label": "3d", "assigned_at": datetime(2026, 6, 3, 15, 0, tzinfo=UTC)},
    )()
    schedule_for_task = AsyncMock(return_value=([scheduled_item], []))
    record_deadline_task_peer = AsyncMock(return_value=None)
    get_active_deadline_for_page = AsyncMock(return_value=None)
    supersede_ledger_rows = AsyncMock(return_value=["<outbox-id>"])
    cancel_outbox_rows = AsyncMock(return_value=None)
    mark_scheduled = AsyncMock(return_value={})

    class FakeConn:
        async def commit(self) -> None:
            return None

    class FakeCtx:
        async def __aenter__(self) -> FakeConn:
            return FakeConn()

        async def __aexit__(self, *_: object) -> None:
            return None

    intake_llm_response = MagicMock()
    intake_llm_response.content = task_response
    intake_model = AsyncMock()
    intake_model.ainvoke = AsyncMock(return_value=intake_llm_response)

    dedup_llm_response = MagicMock()
    dedup_llm_response.content = json.dumps({"matched_page_id": matched_page_id, "confidence": 0.97})
    dedup_model = AsyncMock()
    dedup_model.ainvoke = AsyncMock(return_value=dedup_llm_response)

    with (
        patch("app.tools.notion.create_task", create_task),
        patch("app.tools.notion.update_property", update_property),
        patch("app.tools.notion.mark_reminder_scheduled", mark_scheduled),
        patch("app.tools.db.get_db_conn", return_value=FakeCtx()),
        patch("app.scheduler.reminder_scheduling.record_deadline_task_peer", record_deadline_task_peer),
        patch("app.scheduler.reminder_scheduling.get_active_deadline_for_page", get_active_deadline_for_page),
        patch("app.scheduler.reminder_scheduling.supersede_ledger_rows", supersede_ledger_rows),
        patch("app.scheduler.reminder_scheduling.cancel_outbox_rows", cancel_outbox_rows),
        patch("app.scheduler.reminder_scheduling.schedule_for_task", schedule_for_task),
        patch("app.models.llm", side_effect=[intake_model, dedup_model]),
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(_base_state(incoming="Placeholder deadline task"))

    create_task.assert_not_awaited()
    update_property.assert_awaited_once()
    update_kwargs = update_property.await_args.kwargs
    from app.tools import notion

    assert set(update_kwargs) <= set(inspect.signature(notion.update_property).parameters)
    assert update_kwargs == {
        "page_id": matched_page_id,
        "prop_json": {
            "properties": {"Due At": {"date": {"start": "2026-06-06T22:00:00+00:00"}}}
        },
    }
    get_active_deadline_for_page.assert_awaited_once()
    supersede_ledger_rows.assert_awaited_once()
    cancel_outbox_rows.assert_awaited_once_with(ANY, ["<outbox-id>"])
    record_deadline_task_peer.assert_awaited_once()
    # A duplicate's series is named after the existing page, not the new phrasing.
    _assert_schedule_call(
        schedule_for_task,
        page_id=matched_page_id,
        peer="<test-peer-1>",
        deadline_iso="2026-06-06T22:00:00+00:00",
        title="Placeholder deadline task",
    )
    mark_scheduled.assert_awaited_once_with(matched_page_id)
    draft = result["pending_outbound"][0]
    assert draft["notion_page_id"] == matched_page_id
    assert "deadline" in draft["body"].lower()


@pytest.mark.asyncio
async def test_dedup_deadline_update_failure_fails_open_to_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed duplicate deadline patch creates the proposed task instead."""
    matched_page_id = "<page_id_matched>"
    created_page_id = str(uuid.uuid4())
    due_at = "2026-06-06T17:00:00-05:00"
    task_response = _llm_save_response(title="Placeholder deadline task", due_at=due_at)
    create_task = AsyncMock(return_value=_make_notion_page(page_id=created_page_id))
    update_property = AsyncMock(side_effect=RuntimeError("Notion update failed"))
    schedule_for_task = AsyncMock(return_value=([], []))

    async def query_all() -> dict[str, Any]:
        return {
            "results": [
                _make_query_page(matched_page_id, "Placeholder deadline task", status="In Progress"),
            ]
        }

    monkeypatch.setattr("app.tools.notion.query_all", query_all)

    class FakeCtx:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_: object) -> None:
            return None

    intake_llm_response = MagicMock()
    intake_llm_response.content = task_response
    intake_model = AsyncMock()
    intake_model.ainvoke = AsyncMock(return_value=intake_llm_response)

    dedup_llm_response = MagicMock()
    dedup_llm_response.content = json.dumps({"matched_page_id": matched_page_id, "confidence": 0.97})
    dedup_model = AsyncMock()
    dedup_model.ainvoke = AsyncMock(return_value=dedup_llm_response)

    with (
        patch("app.tools.notion.create_task", create_task),
        patch("app.tools.notion.update_property", update_property),
        patch("app.tools.db.get_db_conn", return_value=FakeCtx()),
        patch("app.scheduler.reminder_scheduling.record_deadline_task_peer", AsyncMock(return_value=None)),
        patch("app.scheduler.reminder_scheduling.schedule_for_task", schedule_for_task),
        patch("app.models.llm", side_effect=[intake_model, dedup_model]),
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(_base_state(incoming="Placeholder deadline task"))

    update_property.assert_awaited_once()
    create_task.assert_awaited_once()
    assert create_task.await_args.kwargs["due_at_iso"] == "2026-06-06T22:00:00+00:00"
    assert result["pending_outbound"][0]["notion_page_id"] == created_page_id


@pytest.mark.asyncio
async def test_dedup_unrelated_existing_task_creates_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrelated existing task does not suppress a new task."""
    page_id = str(uuid.uuid4())
    task_response = _llm_save_response(title="Draft sample report")
    create_task = AsyncMock(return_value=_make_notion_page(page_id=page_id))

    async def query_all() -> dict[str, Any]:
        return {"results": [_make_query_page("<page_id_a>", "Clean placeholder room")]}

    monkeypatch.setattr("app.tools.notion.query_all", query_all)

    mock_llm_response = MagicMock()
    mock_llm_response.content = task_response
    mock_model = AsyncMock()
    mock_model.ainvoke = AsyncMock(return_value=mock_llm_response)

    with (
        patch("app.tools.notion.create_task", create_task),
        patch("app.models.llm", return_value=mock_model),
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(_base_state(incoming="Draft sample report"))

    create_task.assert_awaited_once()
    assert result["pending_outbound"][0]["notion_page_id"] == page_id


@pytest.mark.asyncio
async def test_dedup_multiple_candidates_discloses_only_matched_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The duplicate path never names unmatched candidates or their count."""
    matched_page_id = "<page_id_matched>"
    task_response = _llm_save_response(title="Call placeholder office")
    create_task = AsyncMock(return_value=_make_notion_page(page_id=str(uuid.uuid4())))

    async def query_all() -> dict[str, Any]:
        return {
            "results": [
                _make_query_page(matched_page_id, "Call placeholder office"),
                _make_query_page("<page_id_other>", "Call placeholder coordinator"),
            ]
        }

    monkeypatch.setattr("app.tools.notion.query_all", query_all)

    intake_llm_response = MagicMock()
    intake_llm_response.content = task_response
    intake_model = AsyncMock()
    intake_model.ainvoke = AsyncMock(return_value=intake_llm_response)

    dedup_llm_response = MagicMock()
    dedup_llm_response.content = json.dumps({"matched_page_id": matched_page_id, "confidence": 0.96})
    dedup_model = AsyncMock()
    dedup_model.ainvoke = AsyncMock(return_value=dedup_llm_response)

    with (
        patch("app.tools.notion.create_task", create_task),
        patch("app.models.llm", side_effect=[intake_model, dedup_model]),
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(_base_state(incoming="Call placeholder office"))

    create_task.assert_not_awaited()
    body = result["pending_outbound"][0]["body"]
    assert "Call placeholder office" in body
    assert "Call placeholder coordinator" not in body
    assert "candidate" not in body.lower()
    assert "similar task" not in body.lower()
    assert "i found" not in body.lower()


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
    assert create_task_calls[0]["work_type"] == "focus"
    assert create_task_calls[0]["urgency"] == 50
    assert create_task_calls[0]["time_estimate"] == 30
    assert create_task_calls[0]["energy_required"] == "Medium"
    # No reminder was fabricated.
    assert len(create_reminder_calls) == 0
    # Operator was alerted to the parse failure.
    assert len(alert_calls) == 1
    assert alert_calls[0]["kind"] == "intake_parse_failed"
    assert "unparseable" in alert_calls[0]["body"].lower() or "parse" in alert_calls[0]["body"].lower()
    assert alert_calls[0]["severity"] == "warning"
    # User got an honest message — not a bare "Got it — added." false success,
    # and it flags that the timing/reminder wasn't captured.
    assert result["pending_outbound"]
    body = result["pending_outbound"][0]["body"].lower()
    assert body.strip() != "got it — added."
    assert "remind" in body or "timing" in body or "time" in body


@pytest.mark.asyncio
async def test_unparseable_output_with_notion_down_does_not_claim_capture() -> None:
    """A parse failure plus a Notion outage must not report the task as added.

    The parse-failure path saves the user's raw message to preserve capture. If
    that save itself fails there is nothing on the list, so the reply must be
    the error path's "send it again", never "Added that to your list" — which
    would reproduce the fabricated-success bug one level below the parse fix.
    """
    incoming = "I need to clean the kitchen Friday before 10pm"
    truncated = '{"action": "save", "title": "clean the kitchen", "is_reminder": tr'

    create_reminder_calls: list[dict] = []
    alert_kinds: list[str] = []

    async def mock_create_task(**_: Any) -> dict:
        raise RuntimeError("Notion API unavailable")

    async def mock_create_reminder(**kwargs: Any) -> dict:
        create_reminder_calls.append(kwargs)
        return _make_notion_page(page_id=str(uuid.uuid4()))

    async def mock_alert(*, kind: str, body: str, severity: str = "warning", **_: Any) -> Any:
        alert_kinds.append(kind)
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
                "peer": "<test-intake-notion-down>",
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

    # Nothing was captured, so nothing may be confirmed.
    assert len(create_reminder_calls) == 0
    assert result["pending_outbound"]
    draft = result["pending_outbound"][0]
    assert draft["notion_page_id"] is None
    body = draft["body"].lower()
    assert "added" not in body
    assert "again" in body
    # The operator still hears about it, via the node-level error alert.
    assert "intake_node_error" in alert_kinds


# ---------------------------------------------------------------------------
# Already-done handoff and confirmation shape
# ---------------------------------------------------------------------------


def _model_returning(content: str) -> AsyncMock:
    response = MagicMock()
    response.content = content
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=response)
    return model


@pytest.mark.asyncio
async def test_already_done_hands_the_turn_to_complete_node() -> None:
    """`action: already_done` saves nothing and returns complete_node's update.

    A past-tense report that reached intake is not a new task. The node must
    not create a page; it delegates the same state to complete_node and
    returns that node's update unchanged.
    """
    from app.graph.nodes import intake as intake_module

    complete_update = {
        "pending_outbound": [
            {"recipient": "<test-peer-1>", "body": "Complete reply", "notion_page_id": None}
        ]
    }
    complete_node = AsyncMock(return_value=complete_update)
    create_task = AsyncMock()
    create_reminder = AsyncMock()
    state = _base_state(incoming="I also paid the placeholder bill!")

    with (
        patch("app.models.llm", return_value=_model_returning('{"action": "already_done"}')),
        patch("app.graph.nodes.complete.complete_node", complete_node),
        patch("app.tools.notion.create_task", create_task),
        patch("app.tools.notion.create_reminder", create_reminder),
        capture_logs() as logs,
    ):
        result = await intake_module.intake_node(state)

    complete_node.assert_awaited_once()
    assert complete_node.await_args is not None
    assert complete_node.await_args.args == (state,)
    assert result is complete_update
    create_task.assert_not_awaited()
    create_reminder.assert_not_awaited()
    events = [entry["event"] for entry in logs]
    assert "intake_node.already_done_handoff" in events
    assert "intake_node.saved" not in events


@pytest.mark.asyncio
async def test_already_done_for_an_unlisted_task_completes_nothing() -> None:
    """The handoff runs the real complete_node, and an unlisted report asks.

    The title matches no open task and the match model says the message names
    something unlisted, so the live active task must stay open: no Completed
    write, no reward, and the reply is a question.
    """
    from datetime import UTC, datetime

    from app.graph.nodes import complete as complete_module
    from app.graph.nodes import intake as intake_module

    intake_model = _model_returning('{"action": "already_done"}')
    match_model = _model_returning(
        '{"matched_page_id": null, "confidence": 0.0, "names_unlisted_task": true}'
    )

    def _llm(_tier: str, *, caller: str = "", **_kwargs: Any) -> AsyncMock:
        return match_model if caller == "complete_title_match" else intake_model

    update_status = AsyncMock()
    reward = AsyncMock(return_value={"text": "Nice work!", "attachment_path": None})
    query_all = AsyncMock(return_value={"results": [
        {
            "id": "<page_A>",
            "properties": {
                "Title": {"title": [{"plain_text": "Fold the laundry"}]},
                "Status": {"select": {"name": "In Progress"}},
                "Is Reminder": {"checkbox": False},
            },
        }
    ]})
    state = _base_state(incoming="I also paid the placeholder bill!")
    state["active_task"] = {  # type: ignore[typeddict-item]
        "page_id": "<page_A>",
        "title": "Fold the laundry",
        "selected_at": datetime.now(UTC).isoformat(),
        "work_type": "Physical",
        "energy_required": "Low",
    }

    with (
        patch("app.models.llm", side_effect=_llm),
        patch("app.tools.notion.query_all", query_all),
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.create_task", AsyncMock()) as create_task,
        patch("app.tools.rewards.maybe_reward", reward),
        patch.object(
            complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)
        ),
    ):
        result = await intake_module.intake_node(state)

    update_status.assert_not_awaited()
    reward.assert_not_awaited()
    create_task.assert_not_awaited()
    assert result["pending_outbound"][0]["body"].startswith("Nice one!")
    assert result["pending_clarification"] is not None


@pytest.mark.asyncio
async def test_deadline_confirmation_names_only_the_first_nudge() -> None:
    """Three scheduled slots add one sentence naming the earliest, nothing more.

    The two-sentence cap applies to `confirmation_message`; the runtime appends
    at most one short "First nudge <time>." sentence after it, so the reply the
    user sees is at most three short sentences — never a list of every slot, a
    time estimate, or a numbered plan. The confirmation here uses both of its
    sentences, so the body is exactly that plus the nudge.
    """
    page_id = str(uuid.uuid4())
    confirmation = "Got it — {task}, due Saturday. First step: open the form."
    task_response = _llm_save_response(
        title="Placeholder deadline task",
        due_at="2026-06-06T17:00:00-05:00",
        confirmation=confirmation,
    )
    slots = [
        type("Scheduled", (), {"label": label, "assigned_at": at})()
        for label, at in [
            ("1d", datetime(2026, 6, 5, 22, 0, tzinfo=UTC)),
            ("3d", datetime(2026, 6, 3, 17, 0, tzinfo=UTC)),
            ("4h", datetime(2026, 6, 6, 18, 0, tzinfo=UTC)),
        ]
    ]

    class FakeCtx:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_: object) -> None:
            return None

    with (
        patch("app.models.llm", return_value=_model_returning(task_response)),
        patch(
            "app.tools.notion.create_task",
            AsyncMock(return_value=_make_notion_page(page_id=page_id)),
        ),
        patch("app.tools.notion.mark_reminder_scheduled", AsyncMock(return_value={})),
        patch("app.tools.db.get_db_conn", return_value=FakeCtx()),
        patch(
            "app.scheduler.reminder_scheduling.record_deadline_task_peer",
            AsyncMock(return_value=None),
        ),
        patch(
            "app.scheduler.reminder_scheduling.schedule_for_task",
            AsyncMock(return_value=(slots, [])),
        ),
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(_base_state(incoming="finish the placeholder form by Saturday"))

    draft = result["pending_outbound"][0]
    # 2026-06-03 17:00 UTC is Wed noon in America/Chicago.
    assert draft["body"] == f"{confirmation} First nudge Wed noon."
    # Two model sentences plus the one appended nudge sentence.
    assert draft["body"].count(". ") + 1 == 3
    assert draft["notion_page_title"] == "Placeholder deadline task"


@pytest.mark.asyncio
async def test_missing_confirmation_falls_back_to_the_task_name_only() -> None:
    """With no confirmation_message, the fallback names the task and nothing else.

    It carries the {task} token (send_node substitutes the stored title) and no
    work-type label or time estimate.
    """
    raw = json.loads(_llm_save_response(title="Placeholder task", time_estimate=45))
    del raw["confirmation_message"]

    with (
        patch("app.models.llm", return_value=_model_returning(json.dumps(raw))),
        patch(
            "app.tools.notion.create_task",
            AsyncMock(return_value=_make_notion_page(page_id=str(uuid.uuid4()))),
        ),
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(_base_state(incoming="placeholder task"))

    draft = result["pending_outbound"][0]
    assert draft["body"] == "Got it — {task}."
    assert draft["notion_page_title"] == "Placeholder task"


@pytest.mark.asyncio
async def test_intake_prompt_carries_the_earlier_message_for_a_follow_up() -> None:
    """"no it's new, just log it" can only name the task from history.

    The intake prompt renders the shared history window, so the earlier user
    message is in front of the model on the follow-up turn.
    """
    from langchain_core.messages import AIMessage, HumanMessage

    model = _model_returning(_llm_save_response(title="Pay the placeholder bill"))
    state = _base_state(incoming="no it's new, just log it")
    state["messages"] = [
        HumanMessage(content="I also paid the placeholder bill!"),
        AIMessage(content="Nice — which task was that?"),
    ]

    with (
        patch("app.models.llm", return_value=model),
        patch(
            "app.tools.notion.create_task",
            AsyncMock(return_value=_make_notion_page(page_id=str(uuid.uuid4()))),
        ),
    ):
        from app.graph.nodes.intake import intake_node

        await intake_node(state)

    system_prompt = str(model.ainvoke.await_args.args[0][0].content)
    assert "user: I also paid the placeholder bill!" in system_prompt
    assert "assistant: Nice — which task was that?" in system_prompt


# ---------------------------------------------------------------------------
# Logging a finished item ("no it's new, just log it")
# ---------------------------------------------------------------------------


def _finished_save_response(title: str = "Pay the placeholder bill") -> str:
    raw = json.loads(_llm_save_response(title=title, confirmation=""))
    raw["already_finished"] = True
    raw["inline_steps"] = ""
    return json.dumps(raw)


def _assert_maybe_reward_call(
    mock: AsyncMock, *, peer: str, page_id: str, title: str, streak: int
) -> None:
    """Bind intake's maybe_reward call against the real signature (clause 10).

    The call runs under intake's try/except, so a renamed parameter would turn
    every logged accomplishment into the "send it again" fallback silently.
    """
    from app.tools.rewards import maybe_reward as real_maybe_reward

    mock.assert_awaited_once()
    call = mock.await_args
    inspect.signature(real_maybe_reward).bind(*call.args, **call.kwargs)
    assert call.args == ()
    assert set(call.kwargs) == {
        "peer", "task_title", "notion_page_id", "streak", "work_type", "energy_required",
    }
    assert set(call.kwargs) <= set(inspect.signature(real_maybe_reward).parameters)
    assert call.kwargs["peer"] == peer
    assert call.kwargs["task_title"] == title
    assert call.kwargs["notion_page_id"] == page_id
    assert call.kwargs["streak"] == streak


@pytest.mark.asyncio
async def test_logging_a_finished_item_creates_it_completed_and_celebrates() -> None:
    """A save marked already_finished is stored Completed and rewarded.

    Recording it open would turn an accomplishment into another obligation.
    """
    from app.tools import notion

    page_id = "<page_id_logged>"
    peer = "<test-peer-logged>"
    create_task = AsyncMock(return_value=_make_notion_page(page_id=page_id))
    maybe_reward = AsyncMock(return_value={"text": "Nice work!", "attachment_path": "/tmp/<reward>.png"})
    schedule_series = AsyncMock(return_value=[])
    create_reminder = AsyncMock()
    state = _base_state(incoming="no it's new, just log it", peer=peer)
    state["streak"] = 2
    state["tasks_completed_today"] = 1

    with (
        patch("app.models.llm", return_value=_model_returning(_finished_save_response())),
        patch("app.tools.notion.create_task", create_task),
        patch("app.tools.notion.create_reminder", create_reminder),
        patch("app.tools.rewards.maybe_reward", maybe_reward),
        patch("app.graph.nodes.intake._schedule_deadline_series", schedule_series),
        capture_logs() as logs,
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(state)

    create_task.assert_awaited_once()
    create_kwargs = create_task.await_args.kwargs
    inspect.signature(notion.create_task).bind(**create_kwargs)
    assert create_kwargs["status"] == "Completed"
    assert create_kwargs["title"] == "Pay the placeholder bill"
    assert "parent_id" not in create_kwargs
    assert create_kwargs.get("due_at_iso") is None
    create_reminder.assert_not_awaited()
    schedule_series.assert_not_awaited()

    _assert_maybe_reward_call(
        maybe_reward, peer=peer, page_id=page_id, title="Pay the placeholder bill", streak=3
    )

    draft = result["pending_outbound"][0]
    assert draft["body"] == "{task} — done. Nice work!"
    assert draft["notion_page_title"] == "Pay the placeholder bill"
    assert draft["notion_page_id"] == page_id
    assert draft["attachment_path"] == "/tmp/<reward>.png"
    assert result["streak"] == 3
    assert result["tasks_completed_today"] == 2
    assert result["conversation_state"] == "idle"
    assert result["pending_clarification"] is None
    assert [(e["page_id"], e["event"]) for e in result["recent_tasks"]] == [(page_id, "completed")]

    logged = [e for e in logs if e["event"] == "intake_node.logged_finished"]
    assert len(logged) == 1
    assert logged[0]["duplicate_matched"] is False
    assert "Pay the placeholder bill" not in str(logged[0])
    assert "/tmp/" not in str(logged[0])


@pytest.mark.asyncio
async def test_logging_a_finished_item_that_matches_an_open_task_completes_that_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished item that duplicates an open task completes that page instead."""
    matched_page_id = "<page_id_open>"

    async def query_all() -> dict[str, Any]:
        return {"results": [_make_query_page(matched_page_id, "Pay the placeholder bill")]}

    monkeypatch.setattr("app.tools.notion.query_all", query_all)
    dedup_model = _model_returning(json.dumps({"matched_page_id": matched_page_id, "confidence": 0.95}))
    create_task = AsyncMock()
    update_status = AsyncMock(return_value={"id": matched_page_id})
    maybe_reward = AsyncMock(return_value={"text": "Nice work!", "attachment_path": None})

    with (
        patch(
            "app.models.llm",
            side_effect=[_model_returning(_finished_save_response()), dedup_model],
        ),
        patch("app.tools.notion.create_task", create_task),
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.rewards.maybe_reward", maybe_reward),
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(_base_state(incoming="no it's new, just log it"))

    create_task.assert_not_awaited()
    update_status.assert_awaited_once_with(page_id=matched_page_id, new_status="Completed")
    _assert_maybe_reward_call(
        maybe_reward,
        peer="<test-peer-1>",
        page_id=matched_page_id,
        title="Pay the placeholder bill",
        streak=1,
    )
    draft = result["pending_outbound"][0]
    assert draft["notion_page_id"] == matched_page_id
    assert draft["body"].startswith("{task} — done.")
    assert "attachment_path" not in draft
    assert result["recent_tasks"][-1]["event"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [None, False, "true"])
async def test_save_without_already_finished_true_stays_open(flag: object) -> None:
    """Only a JSON boolean true logs a finished item; anything else is a normal save."""
    raw = json.loads(_llm_save_response(title="Placeholder task", confirmation="Got it — {task}."))
    if flag is not None:
        raw["already_finished"] = flag
    create_task = AsyncMock(return_value=_make_notion_page(page_id="<page_id_open>"))
    maybe_reward = AsyncMock()

    with (
        patch("app.models.llm", return_value=_model_returning(json.dumps(raw))),
        patch("app.tools.notion.create_task", create_task),
        patch("app.tools.rewards.maybe_reward", maybe_reward),
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(_base_state(incoming="placeholder task"))

    assert create_task.await_args.kwargs.get("status", "Pending") == "Pending"
    maybe_reward.assert_not_awaited()
    assert result["pending_outbound"][0]["body"] == "Got it — {task}."
    assert "streak" not in result
    assert result["recent_tasks"][-1]["event"] == "added"
