"""Regression: COMPLETE finds reminders, anchors to the ledger, and names the task.

Four production failures, one resolution path:

  - "Remind me to X", then "Done!" a minute later, got "which task did you
    mean?" — reminder pages were filtered out of every COMPLETE lookup, and
    nothing recorded the page intake had just created.
  - Answering that question with the title typed back still did not resolve.
  - A resolved "done" celebrated without naming what was done.
  - "done" after a deadline nudge rewarded the user and left the task open,
    because the worker recorded the nudge as an already-completed reminder.

The first test runs against real Postgres, because cancelling the reminder's
outbox row is the half the user would only notice when it fired anyway.
"""
from __future__ import annotations

import inspect
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.graph.nodes import complete as complete_module
from app.graph.state import State
from app.tools import notion, rewards

_HAS_DB = bool(os.environ.get("DATABASE_URL", ""))
_REWARD = {"text": "Nice work! ✨", "attachment_path": None}


def _state(incoming: str, **overrides: Any) -> State:
    state: dict[str, Any] = {
        "peer": "<recipient>",
        "incoming": incoming,
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
        "recent_tasks": [],
    }
    state.update(overrides)
    return state  # type: ignore[return-value]


def _ledger(page_id: str, title: str, *, kind: str, event: str, minutes_ago: float) -> dict[str, Any]:
    return {
        "page_id": page_id,
        "title": title,
        "kind": kind,
        "event": event,
        "at": (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat(),
    }


def _notion_page(page_id: str, title: str, *, reminder: bool = False) -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Status": {"select": {"name": "Pending"}},
            "Is Reminder": {"checkbox": reminder},
        },
    }


def _model(content: str) -> AsyncMock:
    response = MagicMock()
    response.content = content
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=response)
    return model


def _assert_completed_write(update_status: AsyncMock, page_id: str) -> None:
    """Validate the write against the real signature (bug class 10)."""
    update_status.assert_awaited_once()
    call = update_status.await_args
    bound = inspect.signature(notion.update_status).bind(*call.args, **call.kwargs)
    assert bound.arguments == {"page_id": page_id, "new_status": "Completed"}


def _assert_reward_kwargs(reward: AsyncMock, page_id: str, title: str) -> None:
    reward.assert_awaited_once()
    kwargs = reward.await_args.kwargs
    assert set(kwargs) <= set(inspect.signature(rewards.maybe_reward).parameters)
    assert kwargs["notion_page_id"] == page_id
    assert kwargs["task_title"] == title


@pytest.fixture()
async def db_conn() -> Any:
    import psycopg

    conn_str = os.environ["DATABASE_URL"]
    async with await psycopg.AsyncConnection.connect(conn_str, autocommit=False) as conn:
        from app.tools.db import _MIGRATIONS_DIR

        for mig in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            await conn.execute(mig.read_text())  # type: ignore[arg-type]
        await conn.commit()
        await conn.execute(
            "TRUNCATE reminder_scheduling_ledger, deadline_task_peers, reminder_outbox, "
            "recent_outbound, ops_alerts_throttle"
        )
        await conn.commit()
        yield conn


@pytest.mark.skipif(not _HAS_DB, reason="DATABASE_URL not set")
@pytest.mark.asyncio
async def test_bare_done_completes_the_reminder_added_a_minute_ago(db_conn: Any) -> None:
    """F1: the reminder has not fired, so only the ledger knows about it."""
    from app.tools import reminders

    page = str(uuid.uuid4())
    peer = f"<recipient-{uuid.uuid4().hex[:8]}>"
    reminder_id = await reminders.enqueue(
        db_conn,
        notion_page_id=page,
        peer=peer,
        body="Test message",
        due_at=datetime.now(UTC) + timedelta(hours=3),
        idempotency_key=f"intake-{page}",
    )
    await db_conn.commit()

    update_status = AsyncMock(return_value={})
    reward = AsyncMock(return_value=_REWARD)
    query_all = AsyncMock()
    llm_factory = MagicMock()
    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", query_all),
        patch("app.tools.rewards.maybe_reward", reward),
        patch("app.models.llm", llm_factory),
    ):
        result = await complete_module.complete_node(
            _state(
                "Done!",
                peer=peer,
                recent_tasks=[
                    _ledger(page, "Take the bins out", kind="reminder", event="added", minutes_ago=1)
                ],
            )
        )

    # A bare "done" with a clear anchor reads neither Notion nor the model.
    query_all.assert_not_awaited()
    llm_factory.assert_not_called()

    _assert_completed_write(update_status, page)
    _assert_reward_kwargs(reward, page, "Take the bins out")
    draft = result["pending_outbound"][0]
    assert draft["notion_page_id"] == page
    assert draft["notion_page_title"] == "Take the bins out"
    assert "which task" not in draft["body"].lower()

    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT state, last_error FROM reminder_outbox WHERE id = %s", (str(reminder_id),)
        )
        row = await cur.fetchone()
    assert row == ("dead", "completed by user"), (
        "the reminder was completed but its outbox row will still fire"
    )
    assert result["recent_tasks"][0]["event"] == "completed"
    assert result["recent_tasks"][0]["title"] == "Take the bins out"


@pytest.mark.asyncio
async def test_a_verbatim_answer_resolves_without_awaiting_the_model() -> None:
    """F2: the title typed back nearly verbatim is an answer, not a question for the model."""
    model = _model(json.dumps({"matched_page_id": None, "confidence": 0.0}))
    update_status = AsyncMock(return_value={})
    pending = {
        "kind": "complete_target",
        "asked_at": datetime.now(UTC).isoformat(),
        "attempts": 1,
        "candidates": [],
    }
    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", AsyncMock(return_value={"results": [
            _notion_page("<page_R>", "Take the bins out", reminder=True),
            _notion_page("<page_A>", "Water the garden"),
        ]})),
        patch("app.tools.rewards.maybe_reward", AsyncMock(return_value=_REWARD)),
        patch.object(complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)),
        patch("app.models.llm", return_value=model),
    ):
        result = await complete_module.complete_node(
            _state("take the bins out", pending_clarification=pending)
        )

    model.ainvoke.assert_not_awaited()
    _assert_completed_write(update_status, "<page_R>")
    assert result["pending_clarification"] is None


@pytest.mark.asyncio
async def test_a_verbatim_standalone_message_still_goes_to_the_model() -> None:
    """The deterministic path is for answers only: "done, now I need to call mom"."""
    model = _model(json.dumps({"matched_page_id": None, "confidence": 0.0}))
    update_status = AsyncMock(return_value={})
    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", AsyncMock(return_value={"results": [
            _notion_page("<page_A>", "Call mom"),
        ]})),
        patch("app.tools.rewards.maybe_reward", AsyncMock(return_value=_REWARD)),
        patch.object(complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)),
        patch("app.models.llm", return_value=model),
    ):
        await complete_module.complete_node(_state("done, now I need to call mom"))

    model.ainvoke.assert_awaited_once()
    update_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_celebration_draft_names_the_task() -> None:
    """F3: the body carries {task} and the draft carries the title send_node fills in."""
    from app.graph.nodes._task_token import render_task_token

    active = {
        "page_id": "<page_A>",
        "title": "Water the plants",
        "selected_at": datetime.now(UTC).isoformat(),
        "work_type": "Physical",
        "energy_required": "Low",
    }
    with (
        patch("app.tools.notion.update_status", AsyncMock(return_value={})),
        patch("app.tools.rewards.maybe_reward", AsyncMock(return_value=_REWARD)),
        patch.object(complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)),
    ):
        result = await complete_module.complete_node(_state("done!", active_task=active))

    draft = result["pending_outbound"][0]
    assert "{task}" in draft["body"]
    assert draft["notion_page_title"] == "Water the plants"
    delivered = render_task_token(draft["body"], title=draft["notion_page_title"])
    assert delivered == "Water the plants — done. Nice work! ✨"


@pytest.mark.asyncio
async def test_done_after_a_deadline_nudge_writes_completed() -> None:
    """F-nudge: a deadline delivery points at a task the worker never completes."""
    nudge = complete_module._CompletionTarget(
        source="recent_outbound",
        page_id="<page_T>",
        task_title="Deadline nudge: Renew the registration. Want one tiny next step?",
        work_type="",
        energy_required="",
        context_at=datetime.now(UTC),
        signal_timestamp=123,
        event="nudged",
        reminder_type="deadline",
    )
    update_status = AsyncMock(return_value={})
    get_page = AsyncMock(return_value=_notion_page("<page_T>", "Renew the registration"))
    clear = AsyncMock()
    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.get_page", get_page),
        patch("app.tools.rewards.maybe_reward", AsyncMock(return_value=_REWARD)),
        patch.object(complete_module, "_load_recent_outbound_target", AsyncMock(return_value=nudge)),
        patch.object(complete_module, "_clear_recent_outbound", clear),
    ):
        result = await complete_module.complete_node(_state("done"))

    _assert_completed_write(update_status, "<page_T>")
    # The ledger had no title, so the page's own title names it — never the nudge body.
    call = get_page.await_args
    assert inspect.signature(notion.get_page).bind(*call.args, **call.kwargs)
    assert result["pending_outbound"][0]["notion_page_title"] == "Renew the registration"
    clear.assert_awaited_once_with(peer="<recipient>", signal_timestamp=123, notion_page_id="<page_T>")


@pytest.mark.asyncio
async def test_done_after_a_rejection_completes_the_offered_alternative() -> None:
    """Rejection leaves no active task; its ledger entries are the only anchor.

    The ledger rejection_node returns feeds complete_node directly, so the
    handoff between the two writers is what is under test.
    """
    from app.graph.nodes.rejection import rejection_node

    declined = {
        "page_id": "<page_A>",
        "title": "Water the plants",
        "status": "In Progress",
        "selected_at": datetime.now(UTC).isoformat(),
    }
    offer = json.dumps({
        "alternative_task_id": "<page_B>",
        "user_message": "Fair — how about {task} instead?",
    })
    with (
        patch("app.tools.notion.query_pending", AsyncMock(return_value={"results": [
            _notion_page("<page_A>", "Water the plants"),
            _notion_page("<page_B>", "Sort the mail"),
        ]})),
        patch("app.tools.notion.update_property", AsyncMock()),
        patch("app.models.llm", return_value=_model(offer)),
    ):
        rejected = await rejection_node(
            _state("not that one", intent="REJECT", active_task=declined)
        )
    assert rejected["active_task"] is None

    update_status = AsyncMock(return_value={})
    llm_factory = MagicMock()
    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", AsyncMock()),
        patch("app.tools.rewards.maybe_reward", AsyncMock(return_value=_REWARD)),
        patch.object(complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)),
        patch("app.models.llm", llm_factory),
    ):
        result = await complete_module.complete_node(
            _state("done", recent_tasks=rejected["recent_tasks"])
        )

    llm_factory.assert_not_called()
    _assert_completed_write(update_status, "<page_B>")
    assert result["pending_outbound"][0]["notion_page_title"] == "Sort the mail"


@pytest.mark.asyncio
async def test_a_bare_done_after_two_ledger_tasks_offers_both() -> None:
    """F8: the clarification names the tasks the conversation just touched."""
    query_all = AsyncMock()
    ledger = [
        _ledger("<page_A>", "Take the recycling out", kind="task", event="added", minutes_ago=2),
        _ledger("<page_B>", "Drop off the return package", kind="task", event="added", minutes_ago=5),
    ]
    update_status = AsyncMock()
    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", query_all),
        patch.object(complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)),
    ):
        result = await complete_module.complete_node(_state("done", recent_tasks=ledger))

    update_status.assert_not_awaited()
    query_all.assert_not_awaited()
    pending = result["pending_clarification"]
    assert [c["page_id"] for c in pending["candidates"]] == ["<page_A>", "<page_B>"]
    body = result["pending_outbound"][0]["body"]
    assert "Take the recycling out" in body and "Drop off the return package" in body
