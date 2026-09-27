"""Integration tests: moving a reminder, and CANNOT_FINISH after a nudge.

Two node paths that lean on the recent-task ledger:

- intake_node moves the reminder a time-only follow-up refers to
  (`reschedule_of`) instead of creating a second page, and swaps its outbox
  row through `reminders.reschedule_for_page`;
- cannot_finish_node anchors to the newest nudged/reminded/suggested ledger
  entry when nothing is active, and writes the model's remaining sub-tasks
  under that page.

The LLM is mocked with exact JSON; Notion is the in-memory `FakeNotion`.
Tests that write the outbox need Postgres (DATABASE_URL) and skip without
it. Placeholder data only.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from app.graph.state import State
from tests.support.notion_fake import FakeNotion

_HAS_DB = bool(os.environ.get("DATABASE_URL", ""))
_needs_db = pytest.mark.skipif(not _HAS_DB, reason="DATABASE_URL not set")

PEER = "<test-reschedule-peer>"


def _model(content: str) -> Any:
    response = MagicMock()
    response.content = content
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=response)
    return model


def _state(**overrides: Any) -> State:
    state: dict[str, Any] = {
        "peer": PEER,
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
    }
    state.update(overrides)
    return state  # type: ignore[return-value]


def _ledger(page_id: str, title: str, *, kind: str, event: str, minutes_ago: float = 1) -> dict:
    return {
        "page_id": page_id,
        "title": title,
        "kind": kind,
        "event": event,
        "at": (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat(),
    }


def _intake_json(*, remind_at: str | None, reschedule_of: str | None,
                 title: str = "Call the pharmacy") -> str:
    return json.dumps({
        "action": "save",
        "title": title,
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
        "confirmation_message": "Got it — I'll remind you at 6pm to {task}.",
        "already_finished": False,
    })


# ---------------------------------------------------------------------------
# reminders.reschedule_for_page
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db_conn() -> Any:
    import psycopg

    from app.tools.db import _MIGRATIONS_DIR

    async with await psycopg.AsyncConnection.connect(
        os.environ["DATABASE_URL"], autocommit=False
    ) as conn:
        for mig in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            await conn.execute(mig.read_text())  # type: ignore[arg-type]
        await conn.commit()
        yield conn


async def _rows(conn: Any, page_id: str) -> list[tuple[str, str, str | None, str, datetime]]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT peer, state, last_error, kind, due_at FROM reminder_outbox "
            "WHERE notion_page_id = %s ORDER BY created_at, due_at",
            (page_id,),
        )
        return [tuple(row) for row in await cur.fetchall()]  # type: ignore[misc]


@_needs_db
@pytest.mark.asyncio
async def test_reschedule_for_page_swaps_only_the_waiting_reminder_row(db_conn: Any) -> None:
    """The waiting reminder row dies; one new row waits for the new time.

    A deadline row belongs to the deadline series and another peer's row is
    not this conversation's, so both stay pending. Moving twice, back to a
    time the reminder held before, needs no unique-key workaround.
    """
    from app.tools import reminders

    page = str(uuid.uuid4())
    five = datetime.now(UTC) + timedelta(hours=2)
    six = five + timedelta(hours=1)
    await reminders.enqueue(
        db_conn, notion_page_id=page, peer=PEER, body="Test message",
        due_at=five, idempotency_key=f"intake-{page}",
    )
    await reminders.enqueue(
        db_conn, notion_page_id=page, peer=PEER, body="Test message",
        due_at=five, idempotency_key=f"deadline-{page}", kind="deadline",
    )
    await reminders.enqueue(
        db_conn, notion_page_id=page, peer="<other-peer>", body="Test message",
        due_at=five, idempotency_key=f"other-{page}",
    )
    await db_conn.commit()

    cancelled, new_id = await reminders.reschedule_for_page(
        db_conn, notion_page_id=page, peer=PEER, body="Test message", due_at=six
    )
    await db_conn.commit()

    assert cancelled == 1
    rows = await _rows(db_conn, page)
    ours = [(state, error, kind, due) for peer, state, error, kind, due in rows if peer == PEER]
    assert ("dead", "rescheduled by user", "reminder", five) in ours
    assert ("pending", None, "deadline", five) in ours
    assert ("pending", None, "reminder", six) in ours
    assert [r for r in rows if r[0] == "<other-peer>"][0][1] == "pending"

    # Move it back to five: the six o'clock row dies, a five o'clock row waits.
    cancelled_again, second_id = await reminders.reschedule_for_page(
        db_conn, notion_page_id=page, peer=PEER, body="Test message", due_at=five
    )
    await db_conn.commit()
    assert cancelled_again == 1
    assert second_id != new_id
    waiting = [
        due for peer, state, _, kind, due in await _rows(db_conn, page)
        if peer == PEER and kind == "reminder" and state == "pending"
    ]
    assert waiting == [five]


# ---------------------------------------------------------------------------
# intake_node — moving a reminder
# ---------------------------------------------------------------------------


@_needs_db
@pytest.mark.asyncio
async def test_intake_moves_the_ledger_reminder_instead_of_creating_one(db_conn: Any) -> None:
    """"actually make it 6pm" updates the page, swaps the outbox row, creates nothing.

    The page was delivered (the worker marks it Completed), so the move also
    reopens it: a Completed page's row would be skipped at delivery.
    """
    from app.graph.nodes.intake import intake_node
    from app.tools import reminders

    fake = FakeNotion()
    page = fake.seed_task(
        title="Call the pharmacy", is_reminder=True, status="Completed",
        reminder_status="sent", remind_at="2026-10-01T17:00:00-05:00",
    )
    five = datetime(2026, 10, 1, 22, 0, tzinfo=UTC)
    await reminders.enqueue(
        db_conn, notion_page_id=page, peer=PEER, body="Test message",
        due_at=five, idempotency_key=f"intake-{page}",
    )
    await db_conn.commit()

    model = _model(_intake_json(remind_at="2026-10-01T18:00:00-05:00", reschedule_of="R1"))
    undo = fake.install()
    try:
        with patch("app.models.llm", return_value=model), capture_logs() as logs:
            result = await intake_node(_state(
                incoming="actually make it 6pm",
                recent_tasks=[_ledger(page, "Call the pharmacy", kind="reminder", event="reminded")],
            ))
    finally:
        undo()

    # The prompt showed the reminder by label, never by page id.
    prompt = str(model.ainvoke.await_args.args[0][0].content)
    assert '- R1: "Call the pharmacy" — set for Thu 2026-10-01 17:00' in prompt
    assert page not in prompt

    assert [w.op for w in fake.writes] == ["update_property"]
    assert fake.pages[page]["remind_at"] == "2026-10-01T23:00:00+00:00"
    assert fake.status_of(page) == "Pending"
    assert fake.pages[page]["reminder_status"] == "pending"
    assert fake.writes[0].payload["properties"]["Completed At"] == {"date": None}

    rows = await _rows(db_conn, page)
    assert [(state, error) for _, state, error, _, _ in rows] == [
        ("dead", "rescheduled by user"),
        ("pending", None),
    ]
    assert rows[1][4] == datetime(2026, 10, 1, 23, 0, tzinfo=UTC)

    draft = result["pending_outbound"][0]
    assert draft["notion_page_id"] == page
    assert draft["notion_page_title"] == "Call the pharmacy"
    assert "{task}" in draft["body"]
    assert result["turn_actions"] == [
        {"action": "notion.update_property", "page_id": page, "status": ""}
    ]
    assert result["recent_tasks"][0]["page_id"] == page
    assert result["recent_tasks"][0]["event"] == "added"
    events = [e["event"] for e in logs]
    assert "intake_node.rescheduled" in events
    assert "intake_node.error" not in events


@pytest.mark.asyncio
async def test_intake_without_reschedule_of_creates_a_new_reminder() -> None:
    """A null `reschedule_of` is a new reminder, even with a candidate shown."""
    from app.graph.nodes.intake import intake_node

    fake = FakeNotion()
    old = fake.seed_task(
        title="Call the pharmacy", is_reminder=True, remind_at="2026-10-01T17:00:00-05:00",
    )
    model = _model(_intake_json(
        remind_at="2026-10-01T20:00:00-05:00", reschedule_of=None, title="Take the bins out",
    ))
    conn_ctx = AsyncMock()
    conn_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
    conn_ctx.__aexit__ = AsyncMock(return_value=None)
    undo = fake.install()
    try:
        with (
            patch("app.models.llm", return_value=model),
            patch("app.tools.db.get_db_conn", return_value=conn_ctx),
            patch("app.tools.reminders.enqueue", AsyncMock(return_value=uuid.uuid4())),
            patch("app.tools.reminders.reschedule_for_page", AsyncMock()) as moved,
        ):
            result = await intake_node(_state(
                incoming="also remind me at 8 to take the bins out",
                recent_tasks=[_ledger(old, "Call the pharmacy", kind="reminder", event="added")],
            ))
    finally:
        undo()

    moved.assert_not_awaited()
    assert [w.op for w in fake.writes] == ["create_reminder"]
    assert fake.pages[old]["remind_at"] == "2026-10-01T17:00:00-05:00"
    assert result["turn_actions"][0]["action"] == "notion.create_reminder"


@pytest.mark.asyncio
async def test_intake_ignores_a_reschedule_label_it_never_showed() -> None:
    """An invented label (or a raw page id) falls through to normal creation."""
    from app.graph.nodes.intake import intake_node

    fake = FakeNotion()
    old = fake.seed_task(
        title="Call the pharmacy", is_reminder=True, remind_at="2026-10-01T17:00:00-05:00",
    )
    conn_ctx = AsyncMock()
    conn_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
    conn_ctx.__aexit__ = AsyncMock(return_value=None)
    for label in ("R7", old):
        fake.writes.clear()
        model = _model(_intake_json(remind_at="2026-10-01T18:00:00-05:00", reschedule_of=label))
        undo = fake.install()
        try:
            with (
                patch("app.models.llm", return_value=model),
                patch("app.tools.db.get_db_conn", return_value=conn_ctx),
                patch("app.tools.reminders.enqueue", AsyncMock(return_value=uuid.uuid4())),
            ):
                await intake_node(_state(
                    incoming="actually make it 6pm",
                    recent_tasks=[_ledger(old, "Call the pharmacy", kind="reminder", event="added")],
                ))
        finally:
            undo()
        assert [w.op for w in fake.writes] == ["create_reminder"], label


@pytest.mark.asyncio
async def test_intake_move_without_a_time_asks_and_writes_nothing() -> None:
    from app.graph.nodes.intake import intake_node

    fake = FakeNotion()
    page = fake.seed_task(
        title="Call the pharmacy", is_reminder=True, remind_at="2026-10-01T17:00:00-05:00",
    )
    model = _model(_intake_json(remind_at=None, reschedule_of="R1"))
    undo = fake.install()
    try:
        with (
            patch("app.models.llm", return_value=model),
            patch("app.tools.reminders.reschedule_for_page", AsyncMock()) as moved,
        ):
            result = await intake_node(_state(
                incoming="actually move it",
                recent_tasks=[_ledger(page, "Call the pharmacy", kind="reminder", event="added")],
            ))
    finally:
        undo()

    moved.assert_not_awaited()
    assert fake.writes == []
    draft = result["pending_outbound"][0]
    assert draft["body"] == "What time should I move {task} to?"
    assert draft["notion_page_title"] == "Call the pharmacy"
    assert result["turn_actions"] == [{"action": "clarify", "page_id": "", "status": ""}]


@pytest.mark.asyncio
async def test_intake_prompt_shows_no_candidates_without_a_ledger_reminder() -> None:
    from app.graph.nodes.intake import intake_node

    fake = FakeNotion()
    model = _model(json.dumps({"action": "clarify", "clarification_question": "Which one?"}))
    undo = fake.install()
    try:
        with patch("app.models.llm", return_value=model):
            await intake_node(_state(incoming="make it 6pm"))
    finally:
        undo()
    prompt = str(model.ainvoke.await_args.args[0][0].content)
    assert "--- BEGIN REMINDER CANDIDATES ---\nNone.\n--- END REMINDER CANDIDATES ---" in prompt


# ---------------------------------------------------------------------------
# cannot_finish_node — anchoring to a nudged task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cannot_finish_after_a_nudge_names_the_task_and_writes_sub_tasks() -> None:
    from app.graph.nodes.cannot_finish import cannot_finish_node

    fake = FakeNotion()
    page = fake.seed_task(
        title="Renew the car registration", time_estimate=45,
        work_type="Independent", energy_required="Low",
    )
    other = fake.seed_task(title="Water the plants")
    model = _model(json.dumps({
        "phase": "analyze_remaining",
        "completed_portion": "printed the form",
        "remaining_sub_tasks": [
            {"title": "Fill in the form", "time_estimate_minutes": 20, "sequence": 1},
            {"title": "Mail the form", "time_estimate_minutes": 15, "sequence": 2},
        ],
        "next_sub_task_message": "Printing it was a real start. Filling in {task} can wait.",
    }))
    undo = fake.install()
    try:
        with patch("app.models.llm", return_value=model), capture_logs() as logs:
            result = await cannot_finish_node(_state(
                incoming="I can't finish that today, I only printed the form",
                intent="CANNOT_FINISH",
                recent_tasks=[
                    _ledger(page, "Renew the car registration", kind="task", event="nudged"),
                    _ledger(other, "Water the plants", kind="task", event="suggested",
                            minutes_ago=30),
                ],
            ))
    finally:
        undo()

    prompt = str(model.ainvoke.await_args.args[0][0].content)
    assert "CURRENT TASK: Renew the car registration" in prompt
    assert "ORIGINAL TIME ESTIMATE: 45 minutes" in prompt

    draft = result["pending_outbound"][0]
    assert draft["notion_page_id"] == page
    assert draft["notion_page_title"] == "Renew the car registration"

    created = [w for w in fake.writes if w.op == "create_task"]
    assert len(created) == 2
    children = [fake.pages[w.page_id] for w in created]
    assert [(c["title"], c["parent_id"], c["sequence"], c["time_estimate"]) for c in children] == [
        ("Fill in the form", page, 1, 20),
        ("Mail the form", page, 2, 15),
    ]
    assert all(c["work_type"] == "Independent" and c["energy_required"] == "Low" for c in children)
    assert not any(w.page_id in (page, other) for w in fake.writes)
    assert [a["action"] for a in result["turn_actions"]] == ["notion.create_task"] * 2
    response_log = next(e for e in logs if e["event"] == "cannot_finish_node.response")
    assert response_log["task_source"] == "ledger"
    assert response_log["created_count"] == 2
    assert "cannot_finish_node.error" not in [e["event"] for e in logs]


@pytest.mark.asyncio
async def test_cannot_finish_asking_progress_writes_nothing() -> None:
    from app.graph.nodes.cannot_finish import cannot_finish_node

    fake = FakeNotion()
    page = fake.seed_task(title="Renew the car registration")
    model = _model(json.dumps({
        "phase": "ask_progress",
        "progress_question": "No worries — what did you get into on it?",
    }))
    undo = fake.install()
    try:
        with patch("app.models.llm", return_value=model):
            result = await cannot_finish_node(_state(
                incoming="I can't finish that today",
                recent_tasks=[_ledger(page, "Renew the car registration", kind="task",
                                      event="nudged")],
            ))
    finally:
        undo()

    assert fake.writes == []
    draft = result["pending_outbound"][0]
    assert draft["notion_page_id"] == page
    assert draft["notion_page_title"] == "Renew the car registration"
    assert result["turn_actions"] == []


@pytest.mark.asyncio
async def test_cannot_finish_active_task_outranks_the_ledger() -> None:
    from app.graph.nodes.cannot_finish import cannot_finish_node

    fake = FakeNotion()
    nudged = fake.seed_task(title="Renew the car registration")
    active = fake.seed_task(title="Sort the mail")
    model = _model(json.dumps({
        "phase": "analyze_remaining",
        "remaining_sub_tasks": [{"title": "Open the envelopes", "time_estimate_minutes": 15}],
        "next_sub_task_message": "Nice start. Next: open the envelopes.",
    }))
    undo = fake.install()
    try:
        with patch("app.models.llm", return_value=model):
            result = await cannot_finish_node(_state(
                incoming="can't do the rest",
                active_task={"page_id": active, "title": "Sort the mail", "time_estimate": 30,
                             "selected_at": datetime.now(UTC).isoformat()},
                recent_tasks=[_ledger(nudged, "Renew the car registration", kind="task",
                                      event="nudged")],
            ))
    finally:
        undo()

    assert result["pending_outbound"][0]["notion_page_id"] == active
    (write,) = fake.writes
    assert fake.pages[write.page_id]["parent_id"] == active


@pytest.mark.asyncio
async def test_cannot_finish_with_no_task_writes_nothing_even_with_sub_tasks() -> None:
    """No active task and no fresh anchor: generic reply, no orphan sub-tasks."""
    from app.graph.nodes.cannot_finish import cannot_finish_node

    fake = FakeNotion()
    stale = fake.seed_task(title="Renew the car registration")
    model = _model(json.dumps({
        "phase": "analyze_remaining",
        "remaining_sub_tasks": [{"title": "Do the next bit", "time_estimate_minutes": 15}],
        "next_sub_task_message": "Nice start.",
    }))
    undo = fake.install()
    try:
        with patch("app.models.llm", return_value=model):
            result = await cannot_finish_node(_state(
                incoming="I can't finish that",
                recent_tasks=[_ledger(stale, "Renew the car registration", kind="task",
                                      event="nudged", minutes_ago=25 * 60)],
            ))
    finally:
        undo()

    assert fake.writes == []
    prompt = str(model.ainvoke.await_args.args[0][0].content)
    assert "CURRENT TASK: your task" in prompt
    draft = result["pending_outbound"][0]
    assert draft["notion_page_id"] is None
    assert "notion_page_title" not in draft
