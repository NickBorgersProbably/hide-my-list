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

import inspect
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


def _intake_json(
    *, remind_at: str | None, reschedule_of: str | None, title: str = "Call the pharmacy"
) -> str:
    return json.dumps(
        {
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
        }
    )


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
        db_conn,
        notion_page_id=page,
        peer=PEER,
        body="Test message",
        due_at=five,
        idempotency_key=f"intake-{page}",
    )
    await reminders.enqueue(
        db_conn,
        notion_page_id=page,
        peer=PEER,
        body="Test message",
        due_at=five,
        idempotency_key=f"deadline-{page}",
        kind="deadline",
    )
    await reminders.enqueue(
        db_conn,
        notion_page_id=page,
        peer="<other-peer>",
        body="Test message",
        due_at=five,
        idempotency_key=f"other-{page}",
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
        due
        for peer, state, _, kind, due in await _rows(db_conn, page)
        if peer == PEER and kind == "reminder" and state == "pending"
    ]
    assert waiting == [five]


# ---------------------------------------------------------------------------
# intake_node — moving a reminder
# ---------------------------------------------------------------------------


@_needs_db
@pytest.mark.asyncio
async def test_intake_moves_the_ledger_reminder_instead_of_creating_one(db_conn: Any) -> None:
    """ "actually make it 6pm" updates the page, swaps the outbox row, creates nothing.

    The page was delivered (the worker marks it Completed), so the move also
    reopens it: a Completed page's row would be skipped at delivery.
    """
    from app.graph.nodes.intake import intake_node
    from app.tools import reminders

    fake = FakeNotion()
    page = fake.seed_task(
        title="Call the pharmacy",
        is_reminder=True,
        status="Completed",
        reminder_status="sent",
        remind_at="2026-10-01T17:00:00-05:00",
    )
    five = datetime(2026, 10, 1, 22, 0, tzinfo=UTC)
    await reminders.enqueue(
        db_conn,
        notion_page_id=page,
        peer=PEER,
        body="Test message",
        due_at=five,
        idempotency_key=f"intake-{page}",
    )
    await db_conn.commit()

    model = _model(_intake_json(remind_at="2026-10-01T18:00:00-05:00", reschedule_of="R1"))
    undo = fake.install()
    try:
        with patch("app.models.llm", return_value=model), capture_logs() as logs:
            result = await intake_node(
                _state(
                    incoming="actually make it 6pm",
                    recent_tasks=[
                        _ledger(page, "Call the pharmacy", kind="reminder", event="reminded")
                    ],
                )
            )
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
        title="Call the pharmacy",
        is_reminder=True,
        remind_at="2026-10-01T17:00:00-05:00",
    )
    model = _model(
        _intake_json(
            remind_at="2026-10-01T20:00:00-05:00",
            reschedule_of=None,
            title="Take the bins out",
        )
    )
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
            result = await intake_node(
                _state(
                    incoming="also remind me at 8 to take the bins out",
                    recent_tasks=[
                        _ledger(old, "Call the pharmacy", kind="reminder", event="added")
                    ],
                )
            )
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
        title="Call the pharmacy",
        is_reminder=True,
        remind_at="2026-10-01T17:00:00-05:00",
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
                await intake_node(
                    _state(
                        incoming="actually make it 6pm",
                        recent_tasks=[
                            _ledger(old, "Call the pharmacy", kind="reminder", event="added")
                        ],
                    )
                )
        finally:
            undo()
        assert [w.op for w in fake.writes] == ["create_reminder"], label


@pytest.mark.asyncio
async def test_intake_move_without_a_time_asks_and_writes_nothing() -> None:
    from app.graph.nodes.intake import intake_node

    fake = FakeNotion()
    page = fake.seed_task(
        title="Call the pharmacy",
        is_reminder=True,
        remind_at="2026-10-01T17:00:00-05:00",
    )
    model = _model(_intake_json(remind_at=None, reschedule_of="R1"))
    undo = fake.install()
    try:
        with (
            patch("app.models.llm", return_value=model),
            patch("app.tools.reminders.reschedule_for_page", AsyncMock()) as moved,
        ):
            result = await intake_node(
                _state(
                    incoming="actually move it",
                    recent_tasks=[
                        _ledger(page, "Call the pharmacy", kind="reminder", event="added")
                    ],
                )
            )
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
        title="Renew the car registration",
        time_estimate=45,
        work_type="Independent",
        energy_required="Low",
    )
    other = fake.seed_task(title="Water the plants")
    model = _model(
        json.dumps(
            {
                "phase": "analyze_remaining",
                "completed_portion": "printed the form",
                "remaining_sub_tasks": [
                    {"title": "Fill in the form", "time_estimate_minutes": 20, "sequence": 1},
                    {"title": "Mail the form", "time_estimate_minutes": 15, "sequence": 2},
                ],
                "next_sub_task_message": "Printing it was a real start. Filling in {task} can wait.",
            }
        )
    )
    undo = fake.install()
    try:
        with patch("app.models.llm", return_value=model), capture_logs() as logs:
            result = await cannot_finish_node(
                _state(
                    incoming="I can't finish that today, I only printed the form",
                    intent="CANNOT_FINISH",
                    recent_tasks=[
                        _ledger(page, "Renew the car registration", kind="task", event="nudged"),
                        _ledger(
                            other,
                            "Water the plants",
                            kind="task",
                            event="suggested",
                            minutes_ago=30,
                        ),
                    ],
                )
            )
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
    model = _model(
        json.dumps(
            {
                "phase": "ask_progress",
                "progress_question": "No worries — what did you get into on it?",
            }
        )
    )
    undo = fake.install()
    try:
        with patch("app.models.llm", return_value=model):
            result = await cannot_finish_node(
                _state(
                    incoming="I can't finish that today",
                    recent_tasks=[
                        _ledger(page, "Renew the car registration", kind="task", event="nudged")
                    ],
                )
            )
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
    model = _model(
        json.dumps(
            {
                "phase": "analyze_remaining",
                "remaining_sub_tasks": [
                    {"title": "Open the envelopes", "time_estimate_minutes": 15}
                ],
                "next_sub_task_message": "Nice start. Next: open the envelopes.",
            }
        )
    )
    undo = fake.install()
    try:
        with patch("app.models.llm", return_value=model):
            result = await cannot_finish_node(
                _state(
                    incoming="can't do the rest",
                    active_task={
                        "page_id": active,
                        "title": "Sort the mail",
                        "time_estimate": 30,
                        "selected_at": datetime.now(UTC).isoformat(),
                    },
                    recent_tasks=[
                        _ledger(nudged, "Renew the car registration", kind="task", event="nudged")
                    ],
                )
            )
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
    model = _model(
        json.dumps(
            {
                "phase": "analyze_remaining",
                "remaining_sub_tasks": [{"title": "Do the next bit", "time_estimate_minutes": 15}],
                "next_sub_task_message": "Nice start.",
            }
        )
    )
    undo = fake.install()
    try:
        with patch("app.models.llm", return_value=model):
            result = await cannot_finish_node(
                _state(
                    incoming="I can't finish that",
                    recent_tasks=[
                        _ledger(
                            stale,
                            "Renew the car registration",
                            kind="task",
                            event="nudged",
                            minutes_ago=25 * 60,
                        )
                    ],
                )
            )
    finally:
        undo()

    assert fake.writes == []
    prompt = str(model.ainvoke.await_args.args[0][0].content)
    assert "CURRENT TASK: your task" in prompt
    draft = result["pending_outbound"][0]
    assert draft["notion_page_id"] is None
    assert "notion_page_title" not in draft


# ---------------------------------------------------------------------------
# Call shapes bound against the real signatures (clause 10)
# ---------------------------------------------------------------------------


def _mock_conn_ctx() -> Any:
    conn = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


@pytest.mark.asyncio
async def test_intake_move_calls_reschedule_for_page_with_its_real_signature() -> None:
    """A renamed parameter of `reschedule_for_page` must fail here, not in the except."""
    from app.graph.nodes.intake import intake_node
    from app.tools import reminders

    real = reminders.reschedule_for_page
    fake = FakeNotion()
    page = fake.seed_task(
        title="Call the pharmacy",
        is_reminder=True,
        remind_at="2026-10-01T17:00:00-05:00",
    )
    moved = AsyncMock(return_value=(1, uuid.uuid4()))
    model = _model(_intake_json(remind_at="2026-10-01T18:00:00-05:00", reschedule_of="R1"))
    undo = fake.install()
    try:
        with (
            patch("app.models.llm", return_value=model),
            patch("app.tools.db.get_db_conn", return_value=_mock_conn_ctx()),
            patch("app.tools.reminders.reschedule_for_page", moved),
        ):
            result = await intake_node(
                _state(
                    incoming="actually make it 6pm",
                    recent_tasks=[
                        _ledger(page, "Call the pharmacy", kind="reminder", event="added")
                    ],
                )
            )
    finally:
        undo()

    moved.assert_awaited_once()
    call = moved.await_args
    assert call is not None
    bound = inspect.signature(inspect.unwrap(real)).bind(*call.args, **call.kwargs)
    assert bound.arguments["notion_page_id"] == page
    assert bound.arguments["peer"] == PEER
    assert bound.arguments["body"] == "Hey — Call the pharmacy"
    assert bound.arguments["due_at"] == datetime(2026, 10, 1, 23, 0, tzinfo=UTC)
    assert result["pending_outbound"][0]["body"] == "Got it — I'll remind you at 6pm to {task}."


@pytest.mark.asyncio
async def test_cannot_finish_calls_create_task_with_its_real_signature() -> None:
    """A renamed `create_task` parameter must fail here, not vanish in the per-sub-task except."""
    from app.graph.nodes.cannot_finish import cannot_finish_node
    from app.tools import notion

    real = notion.create_task
    create_task = AsyncMock(return_value={"id": "<page_child>"})
    page_id = "<page_parent>"
    model = _model(
        json.dumps(
            {
                "phase": "analyze_remaining",
                "remaining_sub_tasks": [
                    {"title": "Fill in the form", "time_estimate_minutes": 20, "sequence": 1},
                ],
                "next_sub_task_message": "Nice start. Next: fill in the form.",
            }
        )
    )
    with (
        patch("app.models.llm", return_value=model),
        patch("app.tools.notion.create_task", create_task),
        patch("app.tools.notion.get_page", AsyncMock(return_value={"properties": {}})),
        capture_logs() as logs,
    ):
        result = await cannot_finish_node(
            _state(
                incoming="can't do the rest",
                recent_tasks=[
                    _ledger(page_id, "Renew the car registration", kind="task", event="nudged")
                ],
            )
        )

    assert "cannot_finish_node.subtask_create_failed" not in [e["event"] for e in logs]
    create_task.assert_awaited_once()
    call = create_task.await_args
    assert call is not None
    bound = inspect.signature(inspect.unwrap(real)).bind(*call.args, **call.kwargs)
    assert bound.arguments["title"] == "Fill in the form"
    assert bound.arguments["parent_id"] == page_id
    assert bound.arguments["sequence"] == 1
    assert bound.arguments["time_estimate"] == 20
    assert result["turn_actions"] == [
        {"action": "notion.create_task", "page_id": "<page_child>", "status": ""}
    ]


# ---------------------------------------------------------------------------
# intake_node — a move whose outbox swap fails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intake_move_outbox_failure_restores_the_page_and_replies_tentatively() -> None:
    """The swap fails: the page goes back to its old time and status, and the
    reply is the tentative one, never "I'll remind you at 6pm"."""
    from app.graph.nodes.intake import RESCHEDULE_FAILED_REPLY, intake_node

    fake = FakeNotion()
    page = fake.seed_task(
        title="Call the pharmacy",
        is_reminder=True,
        status="Completed",
        reminder_status="sent",
        remind_at="2026-10-01T17:00:00-05:00",
    )
    alerts = AsyncMock()
    model = _model(_intake_json(remind_at="2026-10-01T18:00:00-05:00", reschedule_of="R1"))
    undo = fake.install()
    try:
        with (
            patch("app.models.llm", return_value=model),
            patch("app.tools.db.get_db_conn", return_value=_mock_conn_ctx()),
            patch(
                "app.tools.reminders.reschedule_for_page",
                AsyncMock(side_effect=RuntimeError("db down")),
            ),
            patch("app.tools.ops_alerts.enqueue", alerts),
            capture_logs() as logs,
        ):
            result = await intake_node(
                _state(
                    incoming="actually make it 6pm",
                    recent_tasks=[
                        _ledger(page, "Call the pharmacy", kind="reminder", event="reminded")
                    ],
                )
            )
    finally:
        undo()

    # Moved, then put back: the page again says what the outbox still holds.
    assert [w.op for w in fake.writes] == ["update_property", "update_property"]
    assert fake.pages[page]["remind_at"] == "2026-10-01T22:00:00+00:00"
    assert fake.status_of(page) == "Completed"
    assert fake.pages[page]["reminder_status"] == "sent"

    draft = result["pending_outbound"][0]
    assert draft["body"] == RESCHEDULE_FAILED_REPLY
    assert "I'll remind you at" not in draft["body"]
    assert draft["notion_page_title"] == "Call the pharmacy"
    alerts.assert_awaited_once()
    assert alerts.await_args.kwargs["kind"] == "reminder_enqueue_failed"
    events = [e["event"] for e in logs]
    assert "intake_node.reschedule_enqueue_failed" in events
    assert "intake_node.reschedule_restored" in events
    assert "intake_node.rescheduled" not in events
    assert "intake_node.error" not in events


@pytest.mark.asyncio
async def test_intake_move_outbox_failure_still_replies_tentatively_when_restore_fails() -> None:
    from app.graph.nodes.intake import RESCHEDULE_FAILED_REPLY, intake_node

    fake = FakeNotion()
    page = fake.seed_task(
        title="Call the pharmacy",
        is_reminder=True,
        remind_at="2026-10-01T17:00:00-05:00",
    )
    real_update = fake.update_property
    calls = {"n": 0}

    async def _update_then_fail(page_id: str, prop_json: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("notion down")
        return await real_update(page_id, prop_json)

    model = _model(_intake_json(remind_at="2026-10-01T18:00:00-05:00", reschedule_of="R1"))
    undo = fake.install()
    try:
        with (
            patch("app.models.llm", return_value=model),
            patch("app.tools.notion.update_property", _update_then_fail),
            patch("app.tools.db.get_db_conn", return_value=_mock_conn_ctx()),
            patch(
                "app.tools.reminders.reschedule_for_page",
                AsyncMock(side_effect=RuntimeError("db down")),
            ),
            patch("app.tools.ops_alerts.enqueue", AsyncMock()),
            capture_logs() as logs,
        ):
            result = await intake_node(
                _state(
                    incoming="actually make it 6pm",
                    recent_tasks=[
                        _ledger(page, "Call the pharmacy", kind="reminder", event="added")
                    ],
                )
            )
    finally:
        undo()

    assert calls["n"] == 2
    assert result["pending_outbound"][0]["body"] == RESCHEDULE_FAILED_REPLY
    events = [e["event"] for e in logs]
    assert "intake_node.reschedule_restore_failed" in events
    assert "intake_node.error" not in events


@_needs_db
@pytest.mark.asyncio
async def test_a_failed_outbox_swap_rolls_back_and_keeps_the_old_row(db_conn: Any) -> None:
    """With real Postgres: the dead-marking UPDATE rolls back with the failed enqueue."""
    from app.graph.nodes.intake import RESCHEDULE_FAILED_REPLY, intake_node
    from app.tools import reminders

    fake = FakeNotion()
    page = fake.seed_task(
        title="Call the pharmacy",
        is_reminder=True,
        remind_at="2026-10-01T17:00:00-05:00",
    )
    five = datetime(2026, 10, 1, 22, 0, tzinfo=UTC)
    await reminders.enqueue(
        db_conn,
        notion_page_id=page,
        peer=PEER,
        body="Test message",
        due_at=five,
        idempotency_key=f"intake-{page}",
    )
    await db_conn.commit()

    model = _model(_intake_json(remind_at="2026-10-01T18:00:00-05:00", reschedule_of="R1"))
    undo = fake.install()
    try:
        with (
            patch("app.models.llm", return_value=model),
            patch("app.tools.reminders.enqueue", AsyncMock(side_effect=RuntimeError("boom"))),
            patch("app.tools.ops_alerts.enqueue", AsyncMock()),
        ):
            result = await intake_node(
                _state(
                    incoming="actually make it 6pm",
                    recent_tasks=[
                        _ledger(page, "Call the pharmacy", kind="reminder", event="added")
                    ],
                )
            )
    finally:
        undo()

    await db_conn.rollback()
    assert [(state, due) for _, state, _, _, due in await _rows(db_conn, page)] == [
        ("pending", five)
    ]
    assert fake.pages[page]["remind_at"] == "2026-10-01T22:00:00+00:00"
    assert result["pending_outbound"][0]["body"] == RESCHEDULE_FAILED_REPLY


# ---------------------------------------------------------------------------
# intake_node failure path — full kwargs bound against real signatures (clause 10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intake_move_failure_path_kwargs_match_real_signatures() -> None:
    """update_property (forward write + restore) and ops_alerts.enqueue use correct kwargs.

    A renamed parameter in any of these dependencies must cause a loud bind error
    here instead of a silent test pass (mocks swallow all args regardless of names).
    """
    from app.graph.nodes.intake import intake_node
    from app.tools import notion as notion_mod
    from app.tools import ops_alerts as ops_mod

    real_update = notion_mod.update_property
    real_enqueue = ops_mod.enqueue

    update_calls: list[dict] = []

    async def _tracking_update(page_id: str, prop_json: dict) -> dict:
        update_calls.append({"page_id": page_id, "prop_json": prop_json})
        return {}

    alert_mock = AsyncMock(return_value=uuid.uuid4())

    fake = FakeNotion()
    page = fake.seed_task(
        title="Call the pharmacy",
        is_reminder=True,
        status="Completed",
        reminder_status="sent",
        remind_at="2026-10-01T17:00:00-05:00",
    )
    model = _model(_intake_json(remind_at="2026-10-01T18:00:00-05:00", reschedule_of="R1"))
    undo = fake.install()
    try:
        with (
            patch("app.models.llm", return_value=model),
            patch("app.tools.notion.update_property", _tracking_update),
            patch("app.tools.db.get_db_conn", return_value=_mock_conn_ctx()),
            patch(
                "app.tools.reminders.reschedule_for_page",
                AsyncMock(side_effect=RuntimeError("db down")),
            ),
            patch("app.tools.ops_alerts.enqueue", alert_mock),
        ):
            await intake_node(
                _state(
                    incoming="actually make it 6pm",
                    recent_tasks=[
                        _ledger(page, "Call the pharmacy", kind="reminder", event="reminded")
                    ],
                )
            )
    finally:
        undo()

    update_sig = inspect.signature(inspect.unwrap(real_update))

    # Call 1: forward move — sets new time, reopens page, clears Completed At
    assert len(update_calls) >= 1, "Expected at least 1 update_property call (forward)"
    fwd = update_calls[0]
    fwd_bound = update_sig.bind(page_id=fwd["page_id"], prop_json=fwd["prop_json"])
    assert fwd_bound.arguments["page_id"] == page
    fwd_props = fwd_bound.arguments["prop_json"]["properties"]
    assert "Remind At" in fwd_props
    assert fwd_props.get("Completed At") == {"date": None}

    # Call 2: restore — puts page back, including Status
    assert len(update_calls) == 2, f"Expected 2 update_property calls, got {len(update_calls)}"
    rst = update_calls[1]
    rst_bound = update_sig.bind(page_id=rst["page_id"], prop_json=rst["prop_json"])
    assert rst_bound.arguments["page_id"] == page
    rst_props = rst_bound.arguments["prop_json"]["properties"]
    assert "Status" in rst_props
    assert "Remind At" in rst_props

    # Ops alert — full kwargs bound against real signature
    alert_mock.assert_awaited_once()
    alert_sig = inspect.signature(inspect.unwrap(real_enqueue))
    alert_bound = alert_sig.bind(**alert_mock.await_args.kwargs)
    assert alert_bound.arguments["kind"] == "reminder_enqueue_failed"
    assert isinstance(alert_bound.arguments["body"], str) and len(alert_bound.arguments["body"]) > 0


# ---------------------------------------------------------------------------
# design/d-001: restore includes Completed At; null remind_at no longer skips
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intake_move_failure_restore_includes_completed_at() -> None:
    """When the moved page was Completed, the restore write includes Completed At."""
    from app.graph.nodes.intake import intake_node

    restore_calls: list[dict] = []

    fake = FakeNotion()
    page = fake.seed_task(
        title="Call the pharmacy",
        is_reminder=True,
        status="Completed",
        reminder_status="sent",
        remind_at="2026-10-01T17:00:00-05:00",
    )

    call_n = {"n": 0}

    async def _update(page_id: str, prop_json: dict) -> dict:
        call_n["n"] += 1
        if call_n["n"] > 1:
            restore_calls.append({"page_id": page_id, "prop_json": prop_json})
        return {}

    model = _model(_intake_json(remind_at="2026-10-01T18:00:00-05:00", reschedule_of="R1"))
    undo = fake.install()
    try:
        with (
            patch("app.models.llm", return_value=model),
            patch("app.tools.notion.update_property", _update),
            patch("app.tools.db.get_db_conn", return_value=_mock_conn_ctx()),
            patch(
                "app.tools.reminders.reschedule_for_page",
                AsyncMock(side_effect=RuntimeError("db down")),
            ),
            patch("app.tools.ops_alerts.enqueue", AsyncMock()),
        ):
            await intake_node(
                _state(
                    incoming="actually make it 6pm",
                    recent_tasks=[
                        _ledger(page, "Call the pharmacy", kind="reminder", event="reminded")
                    ],
                )
            )
    finally:
        undo()

    assert len(restore_calls) == 1, "Expected exactly one restore call"
    rst_props = restore_calls[0]["prop_json"]["properties"]
    assert "Completed At" in rst_props, "Restore must include Completed At for a previously-Completed page"


@pytest.mark.asyncio
async def test_intake_move_failure_restores_even_with_null_remind_at() -> None:
    """A page whose prior Remind At was null does not skip the restore."""
    from app.graph.nodes.intake import RESCHEDULE_FAILED_REPLY, intake_node

    restore_calls: list[dict] = []
    call_n = {"n": 0}

    # Seed without remind_at so the candidate has remind_at=None
    fake = FakeNotion()
    page = fake.add_task(title="Call the pharmacy", status="Pending", is_reminder=True)

    async def _update(page_id: str, prop_json: dict) -> dict:
        call_n["n"] += 1
        if call_n["n"] > 1:
            restore_calls.append({"page_id": page_id, "prop_json": prop_json})
        return {}

    model = _model(_intake_json(remind_at="2026-10-01T18:00:00-05:00", reschedule_of="R1"))
    undo = fake.install()
    try:
        with (
            patch("app.models.llm", return_value=model),
            patch("app.tools.notion.update_property", _update),
            patch("app.tools.db.get_db_conn", return_value=_mock_conn_ctx()),
            patch(
                "app.tools.reminders.reschedule_for_page",
                AsyncMock(side_effect=RuntimeError("db down")),
            ),
            patch("app.tools.ops_alerts.enqueue", AsyncMock()),
        ):
            result = await intake_node(
                _state(
                    incoming="actually make it 6pm",
                    recent_tasks=[
                        _ledger(page, "Call the pharmacy", kind="reminder", event="added")
                    ],
                )
            )
    finally:
        undo()

    # Even with no original Remind At, the restore still fires (clears it back to null)
    assert len(restore_calls) == 1, "Restore must occur even when prior Remind At was null"
    rst_props = restore_calls[0]["prop_json"]["properties"]
    assert rst_props.get("Remind At") == {"date": None}
    assert result["pending_outbound"][0]["body"] == RESCHEDULE_FAILED_REPLY


# ---------------------------------------------------------------------------
# psych/psy-001: newer ledger anchor beats older active_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cannot_finish_newer_nudge_beats_older_active_task() -> None:
    """A nudge that arrived after the active task was selected wins the anchor race."""
    from app.graph.nodes.cannot_finish import cannot_finish_node

    fake = FakeNotion()
    nudged = fake.seed_task(title="Renew the car registration", time_estimate=45)
    active = fake.seed_task(title="Sort the mail")
    model = _model(
        json.dumps(
            {
                "phase": "ask_progress",
                "progress_question": "No worries — what did you get into on it?",
            }
        )
    )
    undo = fake.install()
    # active_task was selected 10 minutes ago; nudge arrived 1 minute ago
    selected_at = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    try:
        with patch("app.models.llm", return_value=model):
            result = await cannot_finish_node(
                _state(
                    incoming="I can't finish that today",
                    active_task={
                        "page_id": active,
                        "title": "Sort the mail",
                        "time_estimate": 30,
                        "selected_at": selected_at,
                    },
                    recent_tasks=[
                        _ledger(
                            nudged,
                            "Renew the car registration",
                            kind="task",
                            event="nudged",
                            minutes_ago=1,
                        )
                    ],
                )
            )
    finally:
        undo()

    # The nudge arrived more recently, so "that" means the nudged task
    assert result["pending_outbound"][0]["notion_page_id"] == nudged
    assert result["pending_outbound"][0].get("notion_page_title") == "Renew the car registration"


# ---------------------------------------------------------------------------
# psych/psy-002: reminded page is reopened before sub-tasks are added
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cannot_finish_reopens_completed_reminder_page_before_subtasks() -> None:
    """A delivered reminder marks its page Completed; cannot_finish reopens it."""
    from app.graph.nodes.cannot_finish import cannot_finish_node

    fake = FakeNotion()
    page = fake.seed_task(
        title="Send the invoice",
        status="Completed",  # reminder delivery marked it Completed
        time_estimate=30,
        work_type="Independent",
        energy_required="Low",
    )
    model = _model(
        json.dumps(
            {
                "phase": "analyze_remaining",
                "completed_portion": "drafted it",
                "remaining_sub_tasks": [
                    {"title": "Attach PDF", "time_estimate_minutes": 15, "sequence": 1},
                ],
                "next_sub_task_message": "Good start. Next: attach the PDF to {task}.",
            }
        )
    )
    undo = fake.install()
    try:
        with patch("app.models.llm", return_value=model), capture_logs() as logs:
            result = await cannot_finish_node(
                _state(
                    incoming="I can't finish that today, I only drafted it",
                    recent_tasks=[
                        _ledger(page, "Send the invoice", kind="reminder", event="reminded")
                    ],
                )
            )
    finally:
        undo()

    # Page must be reopened (Pending) before sub-tasks are added under it
    assert fake.status_of(page) == "Pending", "Completed reminder page must be reopened"
    writes = [w for w in fake.writes if w.page_id == page]
    assert writes[0].op == "update_property", "First write must be the reopen"
    assert (
        writes[0].payload["properties"]["Status"]["select"]["name"] == "Pending"
    ), "Reopen must set Status to Pending"

    # Sub-tasks are still created under the page
    children = [w for w in fake.writes if w.op == "create_task"]
    assert len(children) == 1
    assert fake.pages[children[0].page_id]["parent_id"] == page

    # The reply names the task
    draft = result["pending_outbound"][0]
    assert draft["notion_page_id"] == page
    assert draft.get("notion_page_title") == "Send the invoice"

    reopen_log = next(
        (e for e in logs if e["event"] == "cannot_finish_node.reopened"), None
    )
    assert reopen_log is not None, "Reopen must be logged"
