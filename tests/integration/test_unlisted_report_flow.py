"""Integration: the recall-free path from an unlisted report to a logged completion.

A past-tense report that matches no open task ("I also paid the placeholder
bill!") is answered with yes/no choices only. These tests drive the real
classify_intent and complete_node across turns, carrying state forward the way
the checkpointer does, with Notion, rewards, and the model stubbed.

- report -> "no" -> "yes": the offered option is declined, the proposed title
  is offered for logging, and "yes" creates a Completed page and rewards it,
  never touching the declined option.
- report -> "yes": "Did you mean <option>?" answered yes completes that option.

Private data discipline: placeholder peers, page ids, and titles only.
"""
from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from app.graph.state import State

_PEER = "<test-peer>"
_OPTION_ID = "<page_option>"
_OPTION_TITLE = "Fold the placeholder laundry"
_REPORT = "I also paid the placeholder bill!"
_PROPOSED = "Pay the placeholder bill"
_LOGGED_ID = "<page_logged>"


def _page(page_id: str, title: str) -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Status": {"select": {"name": "Pending"}},
            "Is Reminder": {"checkbox": False},
        },
    }


def _state(incoming: str) -> State:
    return {  # type: ignore[return-value]
        "peer": _PEER,
        "incoming": incoming,
        "intent": None,
        "messages": [],
        "active_task": {
            "page_id": _OPTION_ID,
            "title": _OPTION_TITLE,
            "selected_at": datetime.now(UTC).isoformat(),
            "work_type": "Physical",
            "energy_required": "Low",
        },
        "streak": 2,
        "tasks_completed_today": 0,
        "user_prefs": {},
        "mood": None,
        "available_minutes": None,
        "conversation_state": "idle",
        "pending_outbound": [],
        "pending_clarification": None,
    }


def _llm_factory(verdict: dict[str, Any]) -> Any:
    """Route each model call by caller; bare yes/no replies must never reach one."""
    calls: list[str] = []

    def factory(_tier: str, **kwargs: Any) -> Any:
        caller = kwargs.get("caller", "")
        calls.append(caller)
        response = MagicMock()
        if caller == "complete_title_match":
            response.content = json.dumps(verdict)
        elif caller == "intake_dedup":
            response.content = json.dumps({"matched_page_id": None, "confidence": 0.0})
        else:
            raise AssertionError(f"unexpected model call from {caller!r}")
        model = AsyncMock()
        model.ainvoke = AsyncMock(return_value=response)
        return model

    factory.calls = calls  # type: ignore[attr-defined]
    return factory


def _advance(state: State, update: dict[str, Any], incoming: str) -> State:
    """Carry a node's update forward and set the next inbound message."""
    merged: dict[str, Any] = dict(state)
    merged.update(update)
    merged["pending_outbound"] = []
    merged["incoming"] = incoming
    return merged  # type: ignore[return-value]


_UNLISTED_VERDICT = {
    "matched_page_id": None,
    "confidence": 0.0,
    "names_unlisted_task": True,
    "unlisted_task_title": _PROPOSED,
}


def _assert_reward_call(mock: AsyncMock, *, page_id: str, title: str, streak: int) -> None:
    """Bind the reward call against the real signature (clause 10)."""
    from app.tools.rewards import maybe_reward as real_maybe_reward

    mock.assert_awaited_once()
    call = mock.await_args
    inspect.signature(real_maybe_reward).bind(*call.args, **call.kwargs)
    assert call.args == ()
    assert set(call.kwargs) == {
        "peer", "task_title", "notion_page_id", "streak", "work_type", "energy_required",
    }
    assert call.kwargs["peer"] == _PEER
    assert call.kwargs["task_title"] == title
    assert call.kwargs["notion_page_id"] == page_id
    assert call.kwargs["streak"] == streak


@pytest.mark.asyncio
async def test_report_yes_logs_the_report_completed_without_touching_the_option() -> None:
    # The report matches no open task and carries a grounded title. The node
    # offers the log question directly (no "Did you mean?" intermediate), so
    # the user reaches a Completed page in two turns instead of four.
    from app.graph.nodes.complete import complete_node
    from app.graph.routing import classify_intent
    from app.tools import notion

    factory = _llm_factory(_UNLISTED_VERDICT)
    update_status = AsyncMock()
    create_task = AsyncMock(return_value={"id": _LOGGED_ID})
    maybe_reward = AsyncMock(return_value={"text": "Nice work!", "attachment_path": None})
    with (
        patch("app.models.llm", side_effect=factory),
        patch(
            "app.tools.notion.query_all",
            AsyncMock(return_value={"results": [_page(_OPTION_ID, _OPTION_TITLE)]}),
        ),
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.create_task", create_task),
        patch("app.tools.rewards.maybe_reward", maybe_reward),
        patch(
            "app.graph.nodes.complete._load_recent_outbound_target",
            AsyncMock(return_value=None),
        ),
        capture_logs() as logs,
    ):
        state = _state(_REPORT)
        asked = await complete_node(state)
        # No "Did you mean?" — the log offer comes first, no candidate stored.
        assert asked["pending_outbound"][0]["body"] == (
            f"Nice one! Want me to log '{_PROPOSED}' as done?"
        )
        assert "notion_page_title" not in asked["pending_outbound"][0]
        assert asked["pending_clarification"]["kind"] == "unlisted_report"
        assert asked["pending_clarification"]["candidates"] == []
        assert asked["pending_clarification"]["title"] == _PROPOSED

        state = _advance(state, asked, "yes")
        routed = await classify_intent(state)
        assert routed["intent"] == "COMPLETE"
        state = _advance(state, routed, "yes")
        logged = await complete_node(state)

    # The context option is never written or rewarded.
    update_status.assert_not_awaited()
    create_task.assert_awaited_once()
    create_kwargs = create_task.await_args.kwargs
    inspect.signature(notion.create_task).bind(**create_kwargs)
    assert create_kwargs["title"] == _PROPOSED
    assert create_kwargs["status"] == "Completed"
    _assert_reward_call(maybe_reward, page_id=_LOGGED_ID, title=_PROPOSED, streak=3)

    draft = logged["pending_outbound"][0]
    assert draft["body"] == "{task} — done. Nice work!"
    assert draft["notion_page_title"] == _PROPOSED
    assert draft["notion_page_id"] == _LOGGED_ID
    assert logged["pending_clarification"] is None
    assert logged["streak"] == 3
    assert logged["tasks_completed_today"] == 1
    assert [(e["page_id"], e["event"]) for e in logged["recent_tasks"]] == [
        (_LOGGED_ID, "completed")
    ]
    # One match call on the report. The yes/no replies never reach a model, and
    # the dedup lookup shortlists nothing against an unrelated open title.
    assert factory.calls == ["complete_title_match"]

    events = [e for e in logs if e["event"] == "complete_node.logged_finished"]
    assert len(events) == 1
    assert _PROPOSED not in str(events[0])
    assert "placeholder" not in str(
        [e for e in logs if e["event"].startswith(("classify_intent.", "complete_node."))]
    )


@pytest.mark.asyncio
async def test_report_yes_logs_proposed_title_and_leaves_option_open() -> None:
    # After the log offer, "yes" logs the proposed title as Completed. The open
    # context task is never completed: the no-candidate clarification record
    # prevents context fallback during the answer.
    from app.graph.nodes.complete import complete_node
    from app.graph.routing import classify_intent
    from app.tools import notion

    factory = _llm_factory(_UNLISTED_VERDICT)
    update_status = AsyncMock()
    create_task = AsyncMock(return_value={"id": _LOGGED_ID})
    maybe_reward = AsyncMock(return_value={"text": "Nice work!", "attachment_path": None})
    with (
        patch("app.models.llm", side_effect=factory),
        patch(
            "app.tools.notion.query_all",
            AsyncMock(return_value={"results": [_page(_OPTION_ID, _OPTION_TITLE)]}),
        ),
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.create_task", create_task),
        patch("app.tools.rewards.maybe_reward", maybe_reward),
        patch(
            "app.graph.nodes.complete._load_recent_outbound_target",
            AsyncMock(return_value=None),
        ),
    ):
        state = _state(_REPORT)
        asked = await complete_node(state)
        state = _advance(state, asked, "yes")
        routed = await classify_intent(state)
        assert routed["intent"] == "COMPLETE"
        state = _advance(state, routed, "yes")
        done = await complete_node(state)

    # Context option untouched; the proposed title is created Completed.
    update_status.assert_not_awaited()
    create_task.assert_awaited_once()
    create_kwargs = create_task.await_args.kwargs
    inspect.signature(notion.create_task).bind(**create_kwargs)
    assert create_kwargs["title"] == _PROPOSED
    assert create_kwargs["status"] == "Completed"
    _assert_reward_call(maybe_reward, page_id=_LOGGED_ID, title=_PROPOSED, streak=3)
    assert done["pending_outbound"][0]["notion_page_title"] == _PROPOSED
    assert done["pending_clarification"] is None
    # "yes" to a no-candidate unlisted_report never triggers another match call.
    assert factory.calls == ["complete_title_match"]
