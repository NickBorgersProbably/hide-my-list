"""Integration tests for the post-send interaction review.

The review is reached the way production reaches it: a `SignalListener`
receives an authorized message, invokes a stub graph whose `ainvoke` returns a
canned final state, and schedules `review_turn` in the background. Notion and
Signal are the shared doubles (`FakeNotion`, `SignalSink`); the model is a
stub returning a fixed verdict; Postgres is real, so the verdict store, the
rate limit, and the reminder cancellation are real round trips.

Covered: a correction that completes a reminder the turn could not place; the
skip when the peer already has a message waiting; cancellation by the peer's
next message; the feature flag; the rate limit; and the `interaction_reviews`
store helpers on their own.

Requires DATABASE_URL. Placeholder data only.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from structlog.testing import capture_logs

from tests.support.notion_fake import FakeNotion
from tests.support.signal_sink import SignalSink

_HAS_DB = bool(os.environ.get("DATABASE_URL", ""))
pytestmark = pytest.mark.skipif(
    not _HAS_DB, reason="DATABASE_URL not set; skipping integration tests"
)

_TITLE = "Renew the library card"


@pytest.fixture(autouse=True)
def _migrated() -> None:
    from app.tools.db import run_migrations

    run_migrations()


@pytest.fixture()
def peer() -> str:
    return f"+1555{uuid.uuid4().int % 10_000_000:07d}"


@pytest.fixture()
def world() -> Any:
    notion = FakeNotion()
    signal = SignalSink()
    undo_notion = notion.install()
    undo_signal = signal.install()
    yield SimpleNamespace(notion=notion, signal=signal)
    undo_signal()
    undo_notion()


class _StubGraph:
    """Returns a canned final state; records checkpoint writes."""

    def __init__(self, final_states: list[dict[str, Any]]) -> None:
        self._final_states = list(final_states)
        self.calls = 0
        self.aupdate_state = AsyncMock()

    async def ainvoke(self, state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return self._final_states[min(self.calls, len(self._final_states)) - 1]

    async def aget_state(self, config: dict[str, Any]) -> Any:
        return SimpleNamespace(config={"configurable": {"checkpoint_id": f"<ckpt-{self.calls}>"}})


def _envelope(peer: str, text: str, timestamp: int) -> dict[str, Any]:
    return {
        "envelope": {
            "source": peer,
            "timestamp": timestamp,
            "dataMessage": {"message": text, "timestamp": timestamp},
        }
    }


def _clarified_state(peer: str, page_id: str) -> dict[str, Any]:
    """A COMPLETE turn that asked "which task?" about a reminder added a minute ago."""
    now = datetime.now(UTC)
    return {
        "peer": peer,
        "incoming": "Done!",
        "intent": "COMPLETE",
        "messages": [
            HumanMessage(content="remind me to renew my library card tomorrow at 9am"),
            AIMessage(content=f"Got it — I'll remind you about {_TITLE} tomorrow at 9am."),
            HumanMessage(content="Done!"),
            AIMessage(content="I can mark that done. Which task did you mean?"),
        ],
        "recent_tasks": [{
            "page_id": page_id, "title": _TITLE, "kind": "reminder", "event": "added",
            "at": (now - timedelta(minutes=1)).isoformat(),
        }],
        "turn_actions": [{"action": "clarify", "page_id": "", "status": ""}],
        "pending_clarification": {
            "kind": "complete_target", "asked_at": now.isoformat(), "attempts": 1,
            "candidates": [],
        },
        "streak": 0,
        "tasks_completed_today": 0,
        "active_task": None,
    }


def _verdict(page_id: str) -> str:
    return json.dumps({
        "verdict": "correct",
        "reason": "The user finished the only open reminder.",
        "action": "complete_task",
        "page_id": page_id,
        "title": None,
        "due": None,
        "follow_up_message": "{task} — marked that one done.",
    })


class _Model:
    """Stub chat model: returns queued verdicts; can block to model a slow call."""

    def __init__(self, *replies: str, block: asyncio.Event | None = None) -> None:
        self._replies = list(replies)
        self.block = block
        self.calls = 0
        self.started = asyncio.Event()

    async def ainvoke(self, _messages: list[Any]) -> Any:
        self.calls += 1
        self.started.set()
        if self.block is not None and self.calls == 1:
            await self.block.wait()
        return SimpleNamespace(content=self._replies[min(self.calls, len(self._replies)) - 1])


def _llm_factory(model: _Model, tiers: list[tuple[str, str | None]]) -> Any:
    def factory(tier: str, *, temperature: float = 0.0, caller: str | None = None) -> _Model:
        tiers.append((tier, caller))
        return model

    return factory


async def _listen(
    listener: Any, inbound: asyncio.Queue[dict[str, Any] | None]
) -> None:
    async def receive(**_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        while True:
            item = await inbound.get()
            if item is None:
                return
            yield item

    with patch("app.ingress.signal_listener.receive_messages", receive):
        await listener.run()


async def _rows(peer: str) -> list[dict[str, Any]]:
    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        cursor = await conn.execute(
            "SELECT * FROM interaction_reviews WHERE peer = %s ORDER BY created_at", (peer,)
        )
        return list(await cursor.fetchall())


async def _seed_outbox(peer: str, page_id: str) -> None:
    from app.tools import reminders
    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        await reminders.enqueue(
            conn,
            notion_page_id=page_id,
            peer=peer,
            body="Test message",
            due_at=datetime.now(UTC) + timedelta(hours=12),
            idempotency_key=f"it-review-{uuid.uuid4()}",
        )
        await conn.commit()


async def _outbox_states(peer: str, page_id: str) -> list[str]:
    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        cursor = await conn.execute(
            "SELECT state FROM reminder_outbox WHERE peer = %s AND notion_page_id = %s",
            (peer, page_id),
        )
        return [row["state"] for row in await cursor.fetchall()]


def _listener(graph: Any, peer: str, **kwargs: Any) -> Any:
    from app.ingress.signal_listener import SignalListener

    return SignalListener(
        account="+15550009999",
        graph=graph,
        authorized_peers=frozenset({peer}),
        message_debounce_seconds=0,
        interaction_review_delay_seconds=kwargs.pop("delay", 0),
        interaction_review_enabled=kwargs.pop("enabled", True),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Correction end to end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_completes_the_reminder_the_turn_could_not_place(
    peer: str, world: Any
) -> None:
    from app.tools import notion as real_notion_module
    from app.tools.rewards import maybe_reward as real_maybe_reward

    page_id = world.notion.seed_task(title=_TITLE, status="Pending", is_reminder=True)
    await _seed_outbox(peer, page_id)
    graph = _StubGraph([_clarified_state(peer, page_id)])
    model = _Model(_verdict(page_id))
    tiers: list[tuple[str, str | None]] = []
    update_calls: list[Any] = []
    original = world.notion.update_status

    async def spy_update_status(*args: Any, **kwargs: Any) -> Any:
        update_calls.append(SimpleNamespace(args=args, kwargs=kwargs))
        return await original(*args, **kwargs)

    reward = AsyncMock(return_value={"text": "🎉", "attachment_path": None})
    listener = _listener(graph, peer)
    inbound: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    inbound.put_nowait(_envelope(peer, "Done!", 1))
    inbound.put_nowait(None)
    with (
        patch("app.models.llm", _llm_factory(model, tiers)),
        patch.object(real_notion_module, "update_status", spy_update_status),
        patch("app.tools.rewards.maybe_reward", reward),
        capture_logs() as logs,
    ):
        await _listen(listener, inbound)
        await listener.wait_for_review(peer)

    assert graph.calls == 1
    assert tiers == [("medium", "interaction_review")]

    # Notion: exactly one write, Completed, shaped for the real client.
    assert len(update_calls) == 1
    call = update_calls[0]
    bound = inspect.signature(real_notion_module.update_status).bind(*call.args, **call.kwargs)
    assert bound.arguments == {"page_id": page_id, "new_status": "Completed"}
    assert world.notion.status_of(page_id) == "Completed"

    # The reminder will not fire afterwards: its pending outbox row is dead.
    assert await _outbox_states(peer, page_id) == ["dead"]

    reward_call = reward.await_args
    assert inspect.signature(real_maybe_reward).bind(
        *reward_call.args, **reward_call.kwargs
    ).arguments == {
        "peer": peer, "task_title": _TITLE, "notion_page_id": page_id, "streak": 1,
    }

    # Exactly one follow-up, naming the task.
    assert len(world.signal.sent) == 1
    follow_up = world.signal.sent[0]
    assert follow_up.recipient == peer
    assert follow_up.body == f"{_TITLE} — marked that one done. 🎉"
    assert follow_up.idempotency_key

    rows = await _rows(peer)
    assert [(r["verdict"], r["action"], r["action_page_id"], r["executed"],
             r["follow_up_sent"], r["intent"], r["turn_ref"]) for r in rows] == [
        ("correct", "complete_task", page_id, True, True, "COMPLETE", "<ckpt-1>"),
    ]

    graph.aupdate_state.assert_awaited_once()
    update = graph.aupdate_state.await_args
    assert update.kwargs == {"as_node": "send"}
    config, values = update.args
    assert config == {"configurable": {"thread_id": peer}}
    assert set(values) == {
        "recent_tasks", "streak", "tasks_completed_today", "conversation_state",
        "pending_clarification", "messages",
    }
    assert values["pending_clarification"] is None
    assert (values["recent_tasks"][0]["page_id"], values["recent_tasks"][0]["event"]) == (
        page_id, "completed",
    )
    assert values["messages"][0].content == follow_up.body

    events = [e["event"] for e in logs]
    assert "interaction_review.corrected" in events
    for entry in logs:
        flat = json.dumps(entry, default=str)
        assert peer not in flat and "library" not in flat.lower()


# ---------------------------------------------------------------------------
# Yielding to the conversation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_is_skipped_when_the_peer_has_a_message_waiting(
    peer: str, world: Any
) -> None:
    from app.ingress.signal_listener import _QueuedMessage

    page_id = world.notion.seed_task(title=_TITLE, status="Pending", is_reminder=True)
    graph = _StubGraph([_clarified_state(peer, page_id)])
    model = _Model(_verdict(page_id))
    listener = _listener(graph, peer)
    # The worker is not running, so the waiting message stays in the buffer.
    await listener._message_buffer.try_add(_QueuedMessage(peer=peer, text="Test message"))
    with patch("app.models.llm", _llm_factory(model, [])), capture_logs() as logs:
        listener._start_review(
            graph=graph, peer=peer, final_state=_clarified_state(peer, page_id),
            config={"configurable": {"thread_id": peer}},
        )
        await listener.wait_for_review(peer)

    assert model.calls == 0
    assert world.notion.writes == []
    assert world.signal.sent == []
    skipped = [e for e in logs if e["event"] == "interaction_review.skipped"]
    assert [e["reason"] for e in skipped] == ["superseded"]
    assert [(r["verdict"], r["reason"], r["executed"]) for r in await _rows(peer)] == [
        ("skipped", "superseded", False),
    ]


@pytest.mark.asyncio
async def test_the_peers_next_message_cancels_the_running_review(
    peer: str, world: Any
) -> None:
    page_id = world.notion.seed_task(title=_TITLE, status="Pending", is_reminder=True)
    later = {**_clarified_state(peer, page_id), "incoming": "never mind", "intent": "CHAT"}
    graph = _StubGraph([_clarified_state(peer, page_id), later])
    ok = json.dumps({
        "verdict": "ok", "reason": "Deferred.", "action": "none", "page_id": None,
        "title": None, "due": None, "follow_up_message": "",
    })
    # The first review's model call hangs until cancelled; the second answers ok.
    model = _Model(_verdict(page_id), ok, block=asyncio.Event())
    listener = _listener(graph, peer)
    inbound: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    inbound.put_nowait(_envelope(peer, "Done!", 1))
    with patch("app.models.llm", _llm_factory(model, [])), capture_logs() as logs:
        runner = asyncio.create_task(_listen(listener, inbound))
        await asyncio.wait_for(model.started.wait(), timeout=5)
        inbound.put_nowait(_envelope(peer, "never mind", 2))
        inbound.put_nowait(None)
        await asyncio.wait_for(runner, timeout=5)
        await listener.wait_for_review(peer)

    assert graph.calls == 2
    assert model.calls == 2
    assert world.notion.writes == []
    assert world.signal.sent == []
    skipped = [e for e in logs if e["event"] == "interaction_review.skipped"]
    assert [e["reason"] for e in skipped] == ["superseded"]
    # The cancelled review stores nothing; the second one stores its ok.
    assert [(r["verdict"], r["action"]) for r in await _rows(peer)] == [("ok", "none")]


@pytest.mark.asyncio
async def test_a_review_that_started_writing_is_awaited_not_cancelled(
    peer: str, world: Any
) -> None:
    page_id = world.notion.seed_task(title=_TITLE, status="Pending", is_reminder=True)
    graph = _StubGraph([_clarified_state(peer, page_id)])
    model = _Model(_verdict(page_id))
    writing = asyncio.Event()
    release = asyncio.Event()
    original = world.notion.update_status

    async def slow_update_status(*args: Any, **kwargs: Any) -> Any:
        writing.set()
        await release.wait()
        return await original(*args, **kwargs)

    order: list[str] = []
    real_ainvoke = graph.ainvoke

    async def tracking_ainvoke(state: dict[str, Any], config: dict[str, Any]) -> Any:
        order.append(f"turn:{state['incoming']}")
        return await real_ainvoke(state, config)

    graph.ainvoke = tracking_ainvoke  # type: ignore[method-assign]
    listener = _listener(graph, peer)
    inbound: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    inbound.put_nowait(_envelope(peer, "Done!", 1))
    with (
        patch("app.models.llm", _llm_factory(model, [])),
        patch("app.tools.notion.update_status", slow_update_status),
        patch("app.tools.rewards.maybe_reward",
              AsyncMock(return_value={"text": "🎉", "attachment_path": None})),
    ):
        runner = asyncio.create_task(_listen(listener, inbound))
        await asyncio.wait_for(writing.wait(), timeout=5)
        inbound.put_nowait(_envelope(peer, "thanks", 2))
        for _ in range(20):
            await asyncio.sleep(0)
        # The next turn waits while the review is mid-write.
        assert order == ["turn:Done!"]
        release.set()
        inbound.put_nowait(None)
        await asyncio.wait_for(runner, timeout=5)
        await listener.wait_for_review(peer)

    assert order == ["turn:Done!", "turn:thanks"]
    assert world.notion.status_of(page_id) == "Completed"
    assert len([m for m in world.signal.sent if _TITLE in m.body]) == 1


@pytest.mark.asyncio
async def test_flag_off_schedules_nothing(peer: str, world: Any) -> None:
    page_id = world.notion.seed_task(title=_TITLE, status="Pending", is_reminder=True)
    graph = _StubGraph([_clarified_state(peer, page_id)])
    model = _Model(_verdict(page_id))
    listener = _listener(graph, peer, enabled=False)
    inbound: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    inbound.put_nowait(_envelope(peer, "Done!", 1))
    inbound.put_nowait(None)
    with patch("app.models.llm", _llm_factory(model, [])):
        await _listen(listener, inbound)
        await listener.wait_for_review(peer)

    assert graph.calls == 1
    assert listener._review_tasks == {}
    assert model.calls == 0
    assert world.signal.sent == []
    assert await _rows(peer) == []


@pytest.mark.asyncio
async def test_rate_limit_reached_skips_before_the_model_call(
    peer: str, world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.tools import interaction_reviews

    monkeypatch.setenv("INTERACTION_REVIEW_MAX_PER_HOUR", "2")
    for _ in range(2):
        await interaction_reviews.insert_review(
            peer=peer, turn_ref="", intent="COMPLETE", verdict="correct", reason="x",
            action="complete_task", action_page_id="<page_prior>", executed=True,
            follow_up_sent=True,
        )
    page_id = world.notion.seed_task(title=_TITLE, status="Pending", is_reminder=True)
    graph = _StubGraph([_clarified_state(peer, page_id)])
    model = _Model(_verdict(page_id))
    listener = _listener(graph, peer)
    inbound: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    inbound.put_nowait(_envelope(peer, "Done!", 1))
    inbound.put_nowait(None)
    with patch("app.models.llm", _llm_factory(model, [])):
        await _listen(listener, inbound)
        await listener.wait_for_review(peer)

    assert model.calls == 0
    assert world.notion.status_of(page_id) == "Pending"
    assert world.signal.sent == []
    assert [(r["verdict"], r["reason"]) for r in await _rows(peer)][-1] == (
        "skipped", "rate_limited",
    )


# ---------------------------------------------------------------------------
# Store round trips
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_review_round_trips_every_column(peer: str) -> None:
    from app.tools import interaction_reviews

    review_id = await interaction_reviews.insert_review(
        peer=peer, turn_ref="<ckpt>", intent="COMPLETE", verdict="correct",
        reason="r" * 600, action="create_task", action_page_id="<page_new>",
        executed=True, follow_up_sent=False,
    )
    rows = await _rows(peer)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == review_id
    assert isinstance(row["created_at"], datetime) and row["created_at"].tzinfo is not None
    assert (row["turn_ref"], row["intent"], row["verdict"], row["action"],
            row["action_page_id"], row["executed"], row["follow_up_sent"]) == (
        "<ckpt>", "COMPLETE", "correct", "create_task", "<page_new>", True, False,
    )
    assert row["reason"] == "r" * interaction_reviews.REASON_MAX_CHARS


@pytest.mark.asyncio
async def test_insert_review_rejects_an_unknown_verdict(peer: str) -> None:
    import psycopg

    from app.tools import interaction_reviews

    with pytest.raises(psycopg.errors.CheckViolation):
        await interaction_reviews.insert_review(
            peer=peer, turn_ref="", intent=None, verdict="maybe",  # type: ignore[arg-type]
            reason="", action=None, action_page_id=None, executed=False,
            follow_up_sent=False,
        )


@pytest.mark.asyncio
async def test_count_executed_corrections_scopes_by_peer_window_and_outcome(peer: str) -> None:
    from app.tools import interaction_reviews
    from app.tools.db import get_db_conn

    other = f"+1555{uuid.uuid4().int % 10_000_000:07d}"
    base = {
        "turn_ref": "", "intent": "COMPLETE", "reason": "", "action_page_id": None,
        "follow_up_sent": True,
    }
    await interaction_reviews.insert_review(
        peer=peer, verdict="correct", action="complete_task", executed=True, **base
    )
    await interaction_reviews.insert_review(  # proposed, never executed
        peer=peer, verdict="correct", action="complete_task", executed=False, **base
    )
    await interaction_reviews.insert_review(
        peer=peer, verdict="ok", action="none", executed=False, **base
    )
    old = await interaction_reviews.insert_review(
        peer=peer, verdict="correct", action="send_only", executed=True, **base
    )
    await interaction_reviews.insert_review(
        peer=other, verdict="correct", action="send_only", executed=True, **base
    )
    async with get_db_conn() as conn:
        await conn.execute(
            "UPDATE interaction_reviews SET created_at = now() - interval '2 hours' WHERE id = %s",
            (old,),
        )
        await conn.commit()

    assert await interaction_reviews.count_executed_corrections(
        peer=peer, window_seconds=3600
    ) == 1
    assert await interaction_reviews.count_executed_corrections(
        peer=peer, window_seconds=3 * 3600
    ) == 2
    everyone = await interaction_reviews.count_executed_corrections(
        peer=None, window_seconds=3600
    )
    assert everyone >= 2
