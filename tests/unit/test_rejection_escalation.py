"""Structural + node coverage for the rejection escalation/distress prompt fix.

docs/ai-prompts/rejection.md, Escalation After Multiple Rejections and
Emotional Distress Detection, require two behaviors the runtime template must
carry:

1. At the 3rd consecutive rejection (and every one after), the reply does not
   suggest another task — it normalizes, then offers a constrained choice
   (describe how you're feeling, or take a break).
2. Self-blame/distress signals get a non-judgmental reframe and an offer to
   step away, rather than another task.

These are prompt-anchor + node-plumbing tests. Behavior correctness (whether
the model actually follows the rule) is covered by the live-LLM eval fixtures
in tests/evals/fixtures/rejection/third_no_normalizes.yaml and
self_blame_gets_exit_ramp.yaml.
"""
from __future__ import annotations

from typing import Any

import pytest


def _rendered_rejection_template(**context: Any) -> str:
    from app.prompts.loader import render_with_defaults

    return render_with_defaults(
        "rejection.md.j2",
        context,
        defaults={
            "task_title": "the suggested task",
            "rejection_reason": "",
            "remaining_tasks_json": "[]",
            "available_minutes": 30,
            "mood": "neutral",
            "conversation_history": "No prior context.",
            "recent_tasks": "None yet.",
            "rejection_streak": 1,
        },
    )


def test_template_carries_third_rejection_no_task_rule() -> None:
    """The escalation section anchors 'no task' + 'constrained choice' language."""
    rendered = _rendered_rejection_template()
    assert "Do not suggest another task" in rendered
    assert "describe how you're feeling, or take a break" in rendered
    assert "Set `alternative_task_id`" in rendered
    assert "to null." in rendered


def test_template_renders_rejection_streak_value() -> None:
    """The rejection_streak variable substitutes into the escalation block."""
    rendered = _rendered_rejection_template(rejection_streak=3)
    assert "REJECTION STREAK: 3" in rendered


def test_template_carries_self_blame_step_away_anchor() -> None:
    """The self-blame row offers to step away, matching docs/ai-prompts/rejection.md."""
    rendered = _rendered_rejection_template()
    assert "Want to step away for a bit?" in rendered
    assert "reframe without judgment and offer" in rendered


def _fresh_iso() -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(minutes=5)).isoformat()


_recent_at = _fresh_iso()


@pytest.mark.parametrize(
    "recent_tasks,expected",
    [
        (None, 0),
        ([], 0),
        # Newest first: a pending suggestion (not itself a rejection), then
        # two rejections in a row.
        (
            [
                {"page_id": "p3", "event": "suggested"},
                {"page_id": "p2", "event": "rejected"},
                {"page_id": "p1", "event": "rejected"},
            ],
            2,
        ),
        # A completed task breaks the streak before older rejections.
        (
            [
                {"page_id": "p3", "event": "suggested"},
                {"page_id": "p2", "event": "rejected"},
                {"page_id": "p1", "event": "completed"},
                {"page_id": "p0", "event": "rejected"},
            ],
            1,
        ),
        # Malformed entries are skipped, not counted or treated as a break.
        (
            ["not-a-dict", {"page_id": "p1", "event": "rejected"}],
            1,
        ),
        # A rejection older than 24 hours belongs to a different sitting: it
        # breaks the streak instead of extending it.
        (
            [
                {"page_id": "p3", "event": "suggested"},
                {"page_id": "p2", "event": "rejected", "at": "2026-01-02T11:00:00+00:00"},
                {"page_id": "p1", "event": "rejected", "at": "2025-12-30T00:00:00+00:00"},
            ],
            1,
        ),
    ],
)
def test_consecutive_rejection_count(recent_tasks: Any, expected: int) -> None:
    from datetime import UTC, datetime

    from app.graph.nodes.rejection import _consecutive_rejection_count

    now = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    assert _consecutive_rejection_count(recent_tasks, now=now) == expected


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _CapturingModel:
    """Records the rendered system prompt for the node's LLM call."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.last_system_prompt: str | None = None

    async def ainvoke(self, messages: list[Any]) -> _FakeResponse:
        self.last_system_prompt = str(messages[0].content)
        return _FakeResponse(self._content)


@pytest.mark.asyncio
async def test_rejection_node_passes_session_streak_to_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rejection_node derives the streak from the ledger and renders it into the prompt."""
    from app import models as models_module
    from app.graph.nodes.rejection import rejection_node
    from app.tools import notion

    async def fake_query_pending() -> dict[str, Any]:
        return {"results": []}

    async def fake_update_property(page_id: str, prop_json: dict[str, Any]) -> dict[str, Any]:
        return {"id": page_id}

    model = _CapturingModel(
        '{"user_message": "Sometimes the brain just is not in task mode. '
        'Want to tell me how you are feeling, or take a break?", '
        '"alternative_task_id": null}'
    )

    monkeypatch.setattr(notion, "query_pending", fake_query_pending)
    monkeypatch.setattr(notion, "update_property", fake_update_property)
    monkeypatch.setattr(models_module, "llm", lambda tier, **kwargs: model)

    await rejection_node(
        {
            "peer": "<recipient>",
            "incoming": "nope not that either",
            "intent": "REJECT",
            "messages": [],
            "active_task": {
                "page_id": "<page-id-003>",
                "title": "Placeholder third task",
                "status": "In Progress",
                "rejection_count": 0,
            },
            # Two prior rejections already in the ledger (newest first):
            # a pending suggestion plus two rejected entries.
            "recent_tasks": [
                {
                    "page_id": "<page-id-003>",
                    "title": "Placeholder third task",
                    "kind": "task",
                    "event": "suggested",
                    "at": _recent_at,
                },
                {
                    "page_id": "<page-id-002>",
                    "title": "Placeholder second task",
                    "kind": "task",
                    "event": "rejected",
                    "at": _recent_at,
                },
                {
                    "page_id": "<page-id-001>",
                    "title": "Placeholder first task",
                    "kind": "task",
                    "event": "rejected",
                    "at": _recent_at,
                },
            ],
            "streak": 0,
            "tasks_completed_today": 0,
            "user_prefs": {},
            "mood": None,
            "available_minutes": 30,
            "conversation_state": "active",
            "pending_outbound": [],
        }
    )

    assert model.last_system_prompt is not None
    assert "REJECTION STREAK: 3" in model.last_system_prompt


def test_template_carries_streak_reset_rule() -> None:
    """Rendered template names all four reset events and the 24-hour cutoff."""
    rendered = _rendered_rejection_template()
    assert "reminded" in rendered
    assert "nudged" in rendered
    assert "24 hours" in rendered


def test_parse_rejection_response_empty_is_shame_safe() -> None:
    """Parser empty-response fallback must not offer another task."""
    from app.graph.nodes.rejection import _parse_rejection_response

    msg, alt_id = _parse_rejection_response("")
    assert "find something" not in msg.lower()
    assert "something different" not in msg.lower()
    assert alt_id is None


@pytest.mark.asyncio
async def test_rejection_node_exception_fallback_is_shame_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exception fallback must not offer another task (safe for any rejection count or distress)."""
    from app.graph.nodes.rejection import rejection_node
    from app.tools import notion

    async def always_raise() -> dict[str, Any]:
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(notion, "query_pending", always_raise)

    result = await rejection_node(
        {
            "peer": "<recipient>",
            "incoming": "no. whats wrong with me",
            "intent": "REJECT",
            "messages": [],
            "active_task": {
                "page_id": "<page-id>",
                "title": "Placeholder task",
                "status": "In Progress",
                "rejection_count": 2,
            },
            "recent_tasks": [],
            "streak": 0,
            "tasks_completed_today": 0,
            "user_prefs": {},
            "mood": None,
            "available_minutes": 30,
            "conversation_state": "active",
            "pending_outbound": [],
        }
    )

    outbound = result.get("pending_outbound", [])
    assert outbound, "exception fallback must produce a reply"
    body = outbound[0].get("body", "")
    assert "find something" not in body.lower()
    assert "something different" not in body.lower()
