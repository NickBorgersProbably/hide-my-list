"""Regression: COMPLETE must not close a task the user never touched.

See README.md. Replays the incident shape: an unresolved reminder for page A
plus a stale checkpointed active_task for page B, and a terse completion reply.
The pre-fix node completed page B and never looked at page A.

Standalone run:
    pytest tests/regressions/bug_0641_complete_wrong_task -v
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.graph.nodes.complete import _ACTIVE_TASK_TTL, _resolve_target, complete_node

_PAGE_A = "<page_a>"  # the reminder the user is answering
_PAGE_B = "<page_b>"  # unrelated task left in the checkpoint


def _reminder(*, sent_ago: timedelta, page_id: str = _PAGE_A) -> dict[str, Any]:
    return {
        "notion_page_id": page_id,
        "signal_timestamp": 1784595243078,
        "title": "<reminder body>",
        "reminder_type": "reminder",
        "sent_at": datetime.now(UTC) - sent_ago,
    }


def _active(*, selected_ago: timedelta | None, page_id: str = _PAGE_B) -> dict[str, Any]:
    task: dict[str, Any] = {
        "page_id": page_id,
        "title": "<task title>",
        "status": "In Progress",
        "work_type": "focus",
        "energy_required": "Medium",
    }
    if selected_ago is not None:
        task["selected_at"] = (datetime.now(UTC) - selected_ago).isoformat()
    return task


# ---------------------------------------------------------------------------
# The incident: both contexts present, reminder is the newer one
# ---------------------------------------------------------------------------

def test_unresolved_reminder_beats_older_active_task() -> None:
    """The exact shape that failed: reminder 3h old, active_task 20h old."""
    target = _resolve_target(
        active_task=_active(selected_ago=timedelta(hours=20)),
        reminder=_reminder(sent_ago=timedelta(hours=3)),
        now=datetime.now(UTC),
    )

    assert target is not None
    assert target["page_id"] == _PAGE_A, (
        "COMPLETE resolved to the checkpointed task instead of the reminder the "
        "user was answering — this closes a task they never touched."
    )
    assert target["source"] == "reminder"


def test_reminder_target_does_not_rewrite_notion() -> None:
    """Reminder pages are already Completed at delivery; a second write is wrong."""
    target = _resolve_target(
        active_task=None,
        reminder=_reminder(sent_ago=timedelta(hours=1)),
        now=datetime.now(UTC),
    )

    assert target is not None
    assert target["needs_notion_completion"] is False


def test_fresh_active_task_beats_older_reminder() -> None:
    """Precedence is by recency, not by source: a just-selected task wins."""
    target = _resolve_target(
        active_task=_active(selected_ago=timedelta(minutes=2)),
        reminder=_reminder(sent_ago=timedelta(hours=6)),
        now=datetime.now(UTC),
    )

    assert target is not None
    assert target["page_id"] == _PAGE_B
    assert target["needs_notion_completion"] is True


# ---------------------------------------------------------------------------
# Staleness guard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "selected_ago",
    [
        pytest.param(_ACTIVE_TASK_TTL + timedelta(minutes=1), id="just-past-ttl"),
        pytest.param(timedelta(days=30), id="ancient"),
    ],
)
def test_stale_active_task_is_not_a_completion_target(selected_ago: timedelta) -> None:
    """Past the TTL, a leftover selection must not be completable."""
    target = _resolve_target(
        active_task=_active(selected_ago=selected_ago),
        reminder=None,
        now=datetime.now(UTC),
    )

    assert target is None, (
        "A stale active_task is still a completion target; an unrelated 'done' "
        "can close a task selected long ago."
    )


def test_active_task_without_selected_at_is_treated_as_stale() -> None:
    """Entries checkpointed before selected_at existed carry no age.

    Those are precisely the long-lived leftovers this guard is for, so an
    unknown age must fail closed rather than default to fresh.
    """
    target = _resolve_target(
        active_task=_active(selected_ago=None),
        reminder=None,
        now=datetime.now(UTC),
    )

    assert target is None


def test_fresh_active_task_inside_ttl_still_completes() -> None:
    """The guard must not break the ordinary select-then-finish flow."""
    target = _resolve_target(
        active_task=_active(selected_ago=_ACTIVE_TASK_TTL - timedelta(minutes=1)),
        reminder=None,
        now=datetime.now(UTC),
    )

    assert target is not None
    assert target["page_id"] == _PAGE_B


# ---------------------------------------------------------------------------
# End-to-end through the node
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_node_does_not_patch_the_stale_page() -> None:
    """The load-bearing assertion: no Notion write lands on the wrong page."""
    update_status = AsyncMock()
    maybe_reward = AsyncMock(
        return_value={"text": "Nice work!", "attachment_path": None}
    )
    clear = AsyncMock(return_value=True)

    state = {
        "peer": "<recipient>",
        "incoming": "done",
        "active_task": _active(selected_ago=timedelta(hours=20)),
        "streak": 4,
    }

    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.rewards.maybe_reward", maybe_reward),
        patch(
            "app.tools.recent_outbound.load_awaiting_reply",
            new_callable=AsyncMock,
            return_value=_reminder(sent_ago=timedelta(hours=3)),
        ),
        patch("app.tools.recent_outbound.clear_awaiting_reply", clear),
    ):
        result = await complete_node(state)  # type: ignore[arg-type]

    update_status.assert_not_awaited()
    assert maybe_reward.await_args.kwargs["notion_page_id"] == _PAGE_A
    assert result["pending_outbound"][0]["notion_page_id"] == _PAGE_A


@pytest.mark.asyncio
async def test_node_clears_the_matched_reminder_row() -> None:
    """An unresolved row must not be able to match a second later reply."""
    clear = AsyncMock(return_value=True)
    reminder = _reminder(sent_ago=timedelta(hours=3))

    state = {"peer": "<recipient>", "incoming": "done", "streak": 0}

    with (
        patch("app.tools.notion.update_status", new_callable=AsyncMock),
        patch(
            "app.tools.rewards.maybe_reward",
            new_callable=AsyncMock,
            return_value={"text": "Nice!", "attachment_path": None},
        ),
        patch(
            "app.tools.recent_outbound.load_awaiting_reply",
            new_callable=AsyncMock,
            return_value=reminder,
        ),
        patch("app.tools.recent_outbound.clear_awaiting_reply", clear),
    ):
        await complete_node(state)  # type: ignore[arg-type]

    clear.assert_awaited_once()
    assert clear.await_args.kwargs["signal_timestamp"] == reminder["signal_timestamp"]


@pytest.mark.asyncio
async def test_node_asks_rather_than_completing_an_unknown_task() -> None:
    """With no usable candidate, ask — never close whatever is in the checkpoint."""
    update_status = AsyncMock()
    maybe_reward = AsyncMock()

    state = {
        "peer": "<recipient>",
        "incoming": "done",
        "active_task": _active(selected_ago=timedelta(days=9)),
        "streak": 2,
    }

    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.rewards.maybe_reward", maybe_reward),
        patch(
            "app.tools.recent_outbound.load_awaiting_reply",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await complete_node(state)  # type: ignore[arg-type]

    update_status.assert_not_awaited()
    maybe_reward.assert_not_awaited()
    assert result["pending_outbound"][0]["notion_page_id"] is None
    assert result["pending_outbound"][0]["body"].strip()
    # The stale entry is dropped so it cannot win a later turn either.
    assert result["active_task"] is None
    assert result.get("streak") is None, "no streak credit for an unidentified task"


@pytest.mark.asyncio
async def test_reminder_lookup_failure_falls_back_to_active_task() -> None:
    """A DB outage must not break completion of a genuinely active task."""
    update_status = AsyncMock()
    maybe_reward = AsyncMock(
        return_value={"text": "Nice work!", "attachment_path": None}
    )

    state = {
        "peer": "<recipient>",
        "incoming": "done",
        "active_task": _active(selected_ago=timedelta(minutes=5)),
        "streak": 1,
    }

    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.rewards.maybe_reward", maybe_reward),
        patch(
            # load_awaiting_reply swallows its own errors and returns None.
            "app.tools.recent_outbound.load_awaiting_reply",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        await complete_node(state)  # type: ignore[arg-type]

    update_status.assert_awaited_once()
    assert update_status.await_args.args[0] == _PAGE_B
