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


def test_parse_rejection_response_valid_json_distress_bypasses_alternative() -> None:
    """Valid JSON with a non-null alternative_task_id is overridden when distress is detected."""
    from app.graph.nodes.rejection import _parse_rejection_response

    valid_json = '{"user_message": "Try this instead: {task}", "alternative_task_id": "<page-id>"}'
    msg, alt_id = _parse_rejection_response(valid_json, incoming="I'm useless")
    assert "nothing" in msg.lower()
    assert alt_id is None


def test_parse_rejection_response_valid_json_streak3_forces_null_alternative() -> None:
    """At streak >= 3, valid JSON with a non-null alternative_task_id has it forced to null."""
    from app.graph.nodes.rejection import _parse_rejection_response

    valid_json = (
        '{"user_message": "Sometimes the brain just isn\'t in task mode.", '
        '"alternative_task_id": "<page-id>"}'
    )
    msg, alt_id = _parse_rejection_response(valid_json, incoming="nope", rejection_streak=3)
    assert alt_id is None
    assert "task mode" in msg.lower()


def test_parse_rejection_response_streak3_suppresses_task_token() -> None:
    """At streak >= 3, a model response with {task} in user_message never reaches the caller."""
    from app.graph.nodes.rejection import _parse_rejection_response

    valid_json = (
        '{"user_message": "Here\'s another one: {task}", '
        '"alternative_task_id": "<page-id>"}'
    )
    msg, alt_id = _parse_rejection_response(valid_json, incoming="nope", rejection_streak=3)
    assert "{task}" not in msg
    assert alt_id is None


@pytest.mark.asyncio
async def test_notion_call_contract_keyword_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_page and update_property are called with keyword arguments; signature stays valid."""
    import inspect
    from unittest.mock import AsyncMock

    from app import models as models_module
    from app.graph.nodes.rejection import rejection_node
    from app.tools import notion as real_notion

    async def fake_query_pending() -> dict[str, Any]:
        return {"results": []}

    get_page_mock = AsyncMock(
        return_value={"id": "<page-id>", "properties": {"Rejection Count": {"number": 2}}}
    )
    update_property_mock = AsyncMock(return_value={"id": "<page-id>"})

    model = _CapturingModel('{"user_message": "No problem.", "alternative_task_id": null}')
    monkeypatch.setattr(real_notion, "query_pending", fake_query_pending)
    monkeypatch.setattr(real_notion, "get_page", get_page_mock)
    monkeypatch.setattr(real_notion, "update_property", update_property_mock)
    monkeypatch.setattr(models_module, "llm", lambda tier, **kwargs: model)

    await rejection_node(
        {
            "peer": "<recipient>",
            "incoming": "not feeling it",
            "intent": "REJECT",
            "messages": [],
            "active_task": None,
            "recent_tasks": [
                {
                    "page_id": "<page-id>",
                    "title": "Placeholder task",
                    "kind": "task",
                    "event": "suggested",
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

    get_page_mock.assert_awaited_once()
    get_page_kwargs = get_page_mock.call_args.kwargs
    assert get_page_kwargs == {"page_id": "<page-id>"}
    inspect.signature(real_notion.get_page).bind(**get_page_kwargs)

    update_property_mock.assert_awaited_once()
    update_kwargs = update_property_mock.call_args.kwargs
    assert update_kwargs["page_id"] == "<page-id>"
    assert update_kwargs["prop_json"]["properties"]["Rejection Count"]["number"] == 3
    inspect.signature(real_notion.update_property).bind(**update_kwargs)


def test_parse_rejection_response_empty_is_shame_safe() -> None:
    """Parser empty-response fallback must not offer another task."""
    from app.graph.nodes.rejection import _parse_rejection_response

    msg, alt_id = _parse_rejection_response("")
    assert "find something" not in msg.lower()
    assert "something different" not in msg.lower()
    assert alt_id is None


def test_parse_rejection_response_empty_with_self_blame_gives_reframe() -> None:
    """Empty/invalid model response + self-blame input → nonjudgmental reframe, no task."""
    from app.graph.nodes.rejection import _parse_rejection_response

    msg, alt_id = _parse_rejection_response("", incoming="whats wrong with me")
    assert "nothing" in msg.lower()
    assert alt_id is None


def test_parse_rejection_response_invalid_json_with_self_blame_gives_reframe() -> None:
    """Unparseable model response + self-blame input → nonjudgmental reframe, no task."""
    from app.graph.nodes.rejection import _parse_rejection_response

    msg, alt_id = _parse_rejection_response("not json at all", incoming="I'm useless")
    assert "nothing" in msg.lower()
    assert alt_id is None


@pytest.mark.asyncio
async def test_rejection_node_exception_fallback_ordinary_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exception fallback for a plain rejection returns generic no-pressure response."""
    from app.graph.nodes.rejection import rejection_node
    from app.tools import notion

    async def always_raise() -> dict[str, Any]:
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(notion, "query_pending", always_raise)

    result = await rejection_node(
        {
            "peer": "<recipient>",
            "incoming": "just not feeling it",
            "intent": "REJECT",
            "messages": [],
            "active_task": {
                "page_id": "<page-id>",
                "title": "Placeholder task",
                "status": "In Progress",
                "rejection_count": 0,
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


@pytest.mark.asyncio
async def test_rejection_node_exception_fallback_self_blame_gives_reframe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exception fallback detects self-blame and returns a nonjudgmental reframe."""
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
    assert "nothing" in body.lower(), "self-blame must receive nonjudgmental reframe"
    assert "find something" not in body.lower()
    assert "something different" not in body.lower()


@pytest.mark.asyncio
async def test_no_active_task_declines_the_last_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With nothing active, "no" declines the alternative offered last turn:
    its stored rejection count is read and bumped, and the ledger records it
    as `rejected` so the streak keeps counting."""
    from app import models as models_module
    from app.graph.nodes.rejection import rejection_node
    from app.tools import notion

    writes: list[tuple[str, int]] = []

    async def fake_query_pending() -> dict[str, Any]:
        return {"results": []}

    async def fake_get_page(page_id: str) -> dict[str, Any]:
        assert page_id == "<page-id-002>"
        return {"id": page_id, "properties": {"Rejection Count": {"number": 3}}}

    async def fake_update_property(page_id: str, prop_json: dict[str, Any]) -> dict[str, Any]:
        writes.append((page_id, prop_json["properties"]["Rejection Count"]["number"]))
        return {"id": page_id}

    model = _CapturingModel(
        '{"user_message": "Your no\'s help me learn — trying something else.", '
        '"alternative_task_id": null}'
    )
    monkeypatch.setattr(notion, "query_pending", fake_query_pending)
    monkeypatch.setattr(notion, "get_page", fake_get_page)
    monkeypatch.setattr(notion, "update_property", fake_update_property)
    monkeypatch.setattr(models_module, "llm", lambda tier, **kwargs: model)

    result = await rejection_node(
        {
            "peer": "<recipient>",
            "incoming": "nah not that one",
            "intent": "REJECT",
            "messages": [],
            "active_task": None,
            "recent_tasks": [
                {
                    "page_id": "<page-id-002>",
                    "title": "Placeholder second task",
                    "kind": "task",
                    "event": "suggested",
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

    assert writes == [("<page-id-002>", 4)]
    assert model.last_system_prompt is not None
    assert "REJECTED TASK: Placeholder second task" in model.last_system_prompt
    assert "REJECTION STREAK: 2" in model.last_system_prompt
    ledger = {e["page_id"]: e["event"] for e in result["recent_tasks"]}
    assert ledger["<page-id-002>"] == "rejected"


def test_declined_suggestion_skips_untitled_and_stale_entries() -> None:
    from datetime import UTC, datetime

    from app.graph.nodes.rejection import _declined_suggestion

    now = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    assert _declined_suggestion(None, now=now) is None
    assert _declined_suggestion(
        [{"page_id": "p", "title": "", "event": "suggested", "at": "2026-01-02T11:00:00+00:00"}],
        now=now,
    ) is None
    assert _declined_suggestion(
        [{"page_id": "p", "title": "T", "event": "suggested", "at": "2025-12-30T00:00:00+00:00"}],
        now=now,
    ) is None
    assert _declined_suggestion(
        [
            {"page_id": "done", "title": "D", "event": "completed", "at": "2026-01-02T11:30:00+00:00"},
            {"page_id": "p", "title": "T", "event": "suggested", "at": "2026-01-02T11:00:00+00:00"},
        ],
        now=now,
    ) == ("p", "T")
