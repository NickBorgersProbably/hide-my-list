"""Integration tests for `hydrate_context`, the graph's entry node.

Reminder deliveries happen outside the graph: `reminder_worker` sends them and
writes `recent_outbound`, and nothing about them reaches the checkpoint. The
hydrate node closes that gap at the start of every turn by merging the peer's
recent deliveries into the `recent_tasks` ledger, so the classifier and the
intent nodes can see "the reminder that just went out".

The DB-backed tests deliver the reminder through the real worker rather than
inserting a `recent_outbound` row by hand: the worker's INSERT is that table's
only production writer, and a fixture insert would keep passing with it gone.

Private data discipline: placeholder peers, page ids, and bodies only.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from tests.support.notion_fake import FakeNotion

_HAS_DB = bool(os.environ.get("DATABASE_URL", ""))
_needs_db = pytest.mark.skipif(not _HAS_DB, reason="DATABASE_URL not set; skipping DB-backed test")


def _mock_llm(content: str) -> Any:
    response = MagicMock()
    response.content = content
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=response)
    return model


@pytest.fixture()
def fake_notion() -> Any:
    """A FakeNotion installed over `app.tools.notion` for the test's duration."""
    fake = FakeNotion()
    undo = fake.install()
    try:
        yield fake
    finally:
        undo()


@pytest.fixture()
async def db_conn() -> Any:
    import psycopg

    async with await psycopg.AsyncConnection.connect(
        os.environ["DATABASE_URL"], autocommit=False
    ) as conn:
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


async def _deliver_reminder(conn: Any, *, peer: str, page_id: str) -> int:
    """Enqueue one due reminder and dispatch it through the real worker."""
    from app.scheduler.reminder_worker import dispatch_due_reminders
    from app.tools import reminders

    await reminders.enqueue(
        conn,
        notion_page_id=page_id,
        peer=peer,
        body="Reminder body placeholder",
        due_at=datetime.now(UTC) - timedelta(minutes=1),
        idempotency_key=f"hydrate-test-{uuid.uuid4()}",
    )
    await conn.commit()

    send = AsyncMock(return_value={"timestamp": 1_700_000_000_123})
    with patch("app.tools.notion.complete_reminder", new_callable=AsyncMock):
        await dispatch_due_reminders(conn, signal_send_fn=send)
    send.assert_awaited_once()
    return 1_700_000_000_123


async def _run_one_turn(graph: Any, *, peer: str, incoming: str) -> dict[str, Any]:
    config = {"configurable": {"thread_id": peer}}
    with (
        patch("app.models.llm", return_value=_mock_llm("CHAT")),
        patch(
            "app.tools.signal_client.send_message",
            new_callable=AsyncMock,
            return_value={"timestamp": 1},
        ),
    ):
        await graph.ainvoke({"peer": peer, "incoming": incoming}, config)
    snapshot = await graph.aget_state(config)
    return dict(snapshot.values)


@_needs_db
@pytest.mark.asyncio
async def test_delivered_reminder_reaches_the_checkpoint_ledger(
    db_conn: Any, fake_notion: FakeNotion
) -> None:
    """A worker delivery shows up as a `reminded` ledger entry on the next turn,
    carrying the page's stored Notion title.
    """
    from langgraph.checkpoint.memory import MemorySaver

    from app.graph.graph import build_graph

    peer = "<recipient-hydrate-1>"
    page_id = fake_notion.seed_task(title="Placeholder reminder task", is_reminder=True)
    await _deliver_reminder(db_conn, peer=peer, page_id=page_id)

    graph = build_graph(checkpointer=MemorySaver())
    with capture_logs() as logs:
        values = await _run_one_turn(graph, peer=peer, incoming="hello")

    ledger = values.get("recent_tasks")
    assert isinstance(ledger, list) and len(ledger) == 1
    entry = ledger[0]
    assert entry["page_id"] == page_id
    assert entry["event"] == "reminded"
    assert entry["kind"] == "reminder"
    # The worker's recent_outbound.title is the sent body, not the task title;
    # the ledger never copies it. The title comes from the Notion page.
    assert entry["title"] == fake_notion.title_of(page_id)
    assert entry["title"] != "Reminder body placeholder"
    assert isinstance(entry["at"], str) and entry["at"]
    assert values["intent"] == "CHAT"
    events = {str(e.get("event")) for e in logs}
    assert "classify_intent.error" not in events
    assert "hydrate_context.recent_outbound_failed" not in events
    assert "hydrate_context.title_lookup_failed" not in events


@_needs_db
@pytest.mark.asyncio
async def test_title_lookup_failure_leaves_the_entry_untitled(db_conn: Any) -> None:
    """A Notion read that fails costs the title, never the merge or the turn.

    The entry is still recorded (untitled), one warning carries the error type
    and counts only, and the classifier runs normally.
    """
    from langgraph.checkpoint.memory import MemorySaver

    from app.graph.graph import build_graph

    peer = "<recipient-hydrate-6>"
    page_id = str(uuid.uuid4())
    await _deliver_reminder(db_conn, peer=peer, page_id=page_id)

    graph = build_graph(checkpointer=MemorySaver())
    get_page = AsyncMock(side_effect=RuntimeError("notion down"))
    with patch("app.tools.notion.get_page", get_page), capture_logs() as logs:
        values = await _run_one_turn(graph, peer=peer, incoming="hello")

    get_page.assert_awaited_once_with(page_id)
    ledger = values["recent_tasks"]
    assert [(e["page_id"], e["title"], e["event"]) for e in ledger] == [
        (page_id, "", "reminded")
    ]
    assert values["intent"] == "CHAT"
    assert values.get("classification_error_fallback") is False
    events = [str(e.get("event")) for e in logs]
    assert "classify_intent.error" not in events
    failures = [e for e in logs if e.get("event") == "hydrate_context.title_lookup_failed"]
    assert len(failures) == 1
    assert failures[0]["error_type"] == ["RuntimeError"]
    assert failures[0]["failed_count"] == 1
    # Private data discipline: no page id, peer, or error message in the event.
    rendered = repr(failures[0])
    assert page_id not in rendered
    assert peer not in rendered
    assert "notion down" not in rendered


@_needs_db
@pytest.mark.asyncio
async def test_title_lookups_are_capped_per_turn(
    db_conn: Any, fake_notion: FakeNotion
) -> None:
    """At most TITLE_LOOKUP_CAP untitled deliveries are read per turn, newest first."""
    from app.graph.context import TITLE_LOOKUP_CAP, hydrate_context

    peer = "<recipient-hydrate-7>"
    page_ids = [
        fake_notion.seed_task(title=f"Placeholder task {n}", is_reminder=True)
        for n in range(TITLE_LOOKUP_CAP + 2)
    ]
    for age_min, page_id in enumerate(page_ids):
        await db_conn.execute(
            """
            INSERT INTO recent_outbound
              (peer, signal_timestamp, notion_page_id, reminder_type, title,
               prompt_kind, sent_at, awaiting_reply, expires_at)
            VALUES (%s, %s, %s, 'reminder', 'Reminder body placeholder', 'sent',
                    now() - make_interval(mins => %s), false,
                    now() + interval '24 hours')
            """,
            (peer, 100 + age_min, page_id, age_min),
        )
    await db_conn.commit()

    get_page = AsyncMock(side_effect=fake_notion.get_page)
    with patch("app.tools.notion.get_page", get_page):
        result = await hydrate_context({"peer": peer, "incoming": "hi"})  # type: ignore[typeddict-item]

    assert get_page.await_count == TITLE_LOOKUP_CAP
    titles = {e["page_id"]: e["title"] for e in result["recent_tasks"]}
    newest = page_ids[:TITLE_LOOKUP_CAP]
    assert all(titles[p] == fake_notion.title_of(p) for p in newest)
    assert all(titles[p] == "" for p in page_ids[TITLE_LOOKUP_CAP:])


@_needs_db
@pytest.mark.asyncio
async def test_known_title_survives_the_delivery_merge(db_conn: Any) -> None:
    """A reminder added earlier keeps its title when its delivery is merged."""
    from langgraph.checkpoint.memory import MemorySaver

    from app.graph.graph import build_graph

    peer = "<recipient-hydrate-2>"
    page_id = str(uuid.uuid4())
    graph = build_graph(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": peer}}
    await graph.aupdate_state(
        config,
        {
            "recent_tasks": [
                {
                    "page_id": page_id,
                    "title": "Take the bins out",
                    "kind": "reminder",
                    "event": "added",
                    "at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                }
            ]
        },
        as_node="send",
    )
    await _deliver_reminder(db_conn, peer=peer, page_id=page_id)

    get_page = AsyncMock()
    with patch("app.tools.notion.get_page", get_page):
        values = await _run_one_turn(graph, peer=peer, incoming="hello")

    ledger = values["recent_tasks"]
    assert len(ledger) == 1
    assert ledger[0]["event"] == "reminded"
    assert ledger[0]["title"] == "Take the bins out"
    # A title the ledger already knows is not read from Notion again.
    get_page.assert_not_awaited()


@_needs_db
@pytest.mark.asyncio
async def test_deadline_delivery_is_recorded_as_nudged(
    db_conn: Any, fake_notion: FakeNotion
) -> None:
    """`reminder_type = 'deadline'` rows map to the `nudged` event.

    Inserted directly because this asserts the row-to-event mapping, not the
    worker's write path (covered above).
    """
    from app.graph.context import hydrate_context

    peer = "<recipient-hydrate-3>"
    page_id = fake_notion.seed_task(title="Placeholder deadline task")
    await db_conn.execute(
        """
        INSERT INTO recent_outbound
          (peer, signal_timestamp, notion_page_id, reminder_type, title,
           prompt_kind, sent_at, awaiting_reply, expires_at)
        VALUES (%s, %s, %s, 'deadline', %s, 'sent', now(), true,
                now() + interval '24 hours')
        """,
        (peer, 42, page_id, "Deadline body placeholder"),
    )
    await db_conn.commit()

    result = await hydrate_context({"peer": peer, "incoming": "hi"})  # type: ignore[typeddict-item]

    assert len(result["recent_tasks"]) == 1
    entry = result["recent_tasks"][0]
    assert entry["event"] == "nudged"
    assert entry["kind"] == "task"
    assert entry["title"] == "Placeholder deadline task"


@_needs_db
@pytest.mark.asyncio
async def test_deliveries_older_than_the_window_are_ignored(db_conn: Any) -> None:
    from app.graph.context import hydrate_context

    peer = "<recipient-hydrate-4>"
    await db_conn.execute(
        """
        INSERT INTO recent_outbound
          (peer, signal_timestamp, notion_page_id, reminder_type, title,
           prompt_kind, sent_at, awaiting_reply, expires_at)
        VALUES (%s, %s, %s, 'reminder', '', 'sent', now() - interval '8 days', false,
                now() - interval '7 days')
        """,
        (peer, 43, str(uuid.uuid4())),
    )
    await db_conn.commit()

    result = await hydrate_context({"peer": peer, "incoming": "hi"})  # type: ignore[typeddict-item]
    assert result["recent_tasks"] == []


@pytest.mark.asyncio
async def test_db_failure_keeps_the_existing_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreachable Postgres costs the merge, never the turn.

    The existing ledger comes back pruned, one warning is logged with the error
    type only, and the node does not raise into classify_intent.
    """
    from app.graph.context import hydrate_context

    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/unreachable")
    fresh = {
        "page_id": "<page_fresh>",
        "title": "Take the bins out",
        "kind": "reminder",
        "event": "added",
        "at": (datetime.now(UTC) - timedelta(minutes=2)).isoformat(),
    }
    stale = {**fresh, "page_id": "<page_stale>", "at": (datetime.now(UTC) - timedelta(days=9)).isoformat()}

    with capture_logs() as logs:
        result = await hydrate_context(
            {"peer": "<recipient>", "incoming": "hi", "recent_tasks": [fresh, stale]}  # type: ignore[typeddict-item]
        )

    assert result == {"recent_tasks": [fresh], "turn_actions": []}
    failures = [e for e in logs if e.get("event") == "hydrate_context.recent_outbound_failed"]
    assert len(failures) == 1
    failure = failures[0]
    assert failure.get("error_type")
    assert failure.get("existing_count") == 2
    # Private data discipline: no titles, peers, or row contents in the event.
    rendered = repr(failure)
    assert "Take the bins out" not in rendered
    assert "<recipient>" not in rendered


@pytest.mark.asyncio
async def test_db_failure_does_not_trip_the_classifier_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole graph still classifies and replies when hydrate cannot read Postgres."""
    from langgraph.checkpoint.memory import MemorySaver

    from app.graph.graph import build_graph

    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/unreachable")
    graph = build_graph(checkpointer=MemorySaver())

    with capture_logs() as logs:
        values = await _run_one_turn(graph, peer="<recipient-hydrate-5>", incoming="hello")

    events = [str(e.get("event")) for e in logs]
    assert "classify_intent.error" not in events
    assert "hydrate_context.recent_outbound_failed" in events
    assert values["intent"] == "CHAT"
    assert values.get("classification_error_fallback") is False
    assert values.get("recent_tasks") == []


@pytest.mark.asyncio
async def test_no_database_configured_is_a_quiet_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.graph.context import hydrate_context

    monkeypatch.delenv("DATABASE_URL", raising=False)
    with capture_logs() as logs:
        result = await hydrate_context({"peer": "<recipient>", "incoming": "hi"})  # type: ignore[typeddict-item]
    assert result == {"recent_tasks": [], "turn_actions": []}
    assert not [e for e in logs if str(e.get("event", "")).endswith("_failed")]


def test_graph_entry_point_is_hydrate_context() -> None:
    """hydrate_context runs before the classifier on every turn."""
    from app.graph.graph import build_graph

    graph = build_graph()
    drawable = graph.get_graph()
    start_edges = [e for e in drawable.edges if e.source == "__start__"]
    assert [e.target for e in start_edges] == ["hydrate_context"]
    hydrate_edges = [e for e in drawable.edges if e.source == "hydrate_context"]
    assert [e.target for e in hydrate_edges] == ["classify_intent"]
