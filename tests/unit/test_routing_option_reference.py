"""Positional replies to an open completion clarification skip the classifier.

While ``pending_clarification`` is live, a whole-message positional or yes/no
reply ("the first one", "yes") routes to COMPLETE without a model call and
keeps the record, so complete_node can resolve the option it points at. Any
other text, or the same text with no live clarification, goes to the model.

Private data discipline: placeholder peers, page ids, and titles only.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from app.graph.state import State

_POSITIONAL_REPLIES = [
    "the first one",
    "The first one!",
    "first",
    "second one",
    "the 2nd one",
    "3rd",
    "the last one",
    "the former",
    "latter",
    "1",
    "number 2",
    "that one",
    "this one.",
    "yes",
    "Yeah",
    "yep",
    "  the   second  one  ",
]

_NEGATIVE_REPLIES = [
    "no",
    "nope",
    "neither",
    "none",
    "none of them",
    "none of those",
    "No",
    "NOPE",
    "None of them!",
]


def _clarification(asked_at: datetime | None = None, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "kind": "complete_target",
        "asked_at": (asked_at or datetime.now(UTC)).isoformat(),
        "attempts": 1,
        "candidates": [
            {"page_id": "<page-id-1>", "title": "Test task one"},
            {"page_id": "<page-id-2>", "title": "Test task two"},
        ],
    }
    record.update(overrides)
    return record


def _state(incoming: str, pending: Any = None) -> State:
    state: State = {
        "peer": "<test-peer>",
        "incoming": incoming,
        "intent": "CHAT",
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
    if pending is not None:
        state["pending_clarification"] = pending  # type: ignore[typeddict-item]
    return state


class _RecordingLLM:
    """Stands in for app.models.llm; counts calls and returns a fixed label."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.calls = 0

    def __call__(self, _tier: str, **_kwargs: Any) -> Any:
        outer = self

        class _Resp:
            content = outer.label

        class _Model:
            async def ainvoke(self, _msgs: list[Any]) -> Any:
                outer.calls += 1
                return _Resp()

        return _Model()


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", _POSITIONAL_REPLIES)
async def test_positional_reply_with_live_clarification_routes_complete_without_model(
    reply: str,
) -> None:
    from app.graph import routing

    pending = _clarification()
    fake = _RecordingLLM("ADD_TASK")
    with patch("app.models.llm", new=fake):
        result = await routing.classify_intent(_state(reply, pending))

    assert fake.calls == 0
    assert result["intent"] == "COMPLETE"
    assert result["pending_clarification"] == pending
    assert result["classification_error_fallback"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["the first one", "yes", "that one", "no", "nope"])
async def test_positional_reply_without_clarification_goes_to_model(reply: str) -> None:
    from app.graph import routing

    fake = _RecordingLLM("CHAT")
    with patch("app.models.llm", new=fake):
        result = await routing.classify_intent(_state(reply))

    assert fake.calls == 1
    assert result["intent"] == "CHAT"
    assert result["pending_clarification"] is None


@pytest.mark.asyncio
async def test_log_new_reply_still_goes_to_model_and_add_task_drops_clarification() -> None:
    from app.graph import routing

    fake = _RecordingLLM("ADD_TASK")
    with patch("app.models.llm", new=fake):
        result = await routing.classify_intent(
            _state("no it's new, just log it", _clarification())
        )

    assert fake.calls == 1
    assert result["intent"] == "ADD_TASK"
    assert result["pending_clarification"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    ["the first one was the garden", "yes the first", "first thing tomorrow", "no thanks, add it"],
)
async def test_partial_matches_are_not_option_references(reply: str) -> None:
    from app.graph import routing

    fake = _RecordingLLM("CHAT")
    with patch("app.models.llm", new=fake):
        await routing.classify_intent(_state(reply, _clarification()))

    assert fake.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", _NEGATIVE_REPLIES)
async def test_negative_reply_with_live_clarification_clears_it_without_completion(
    reply: str,
) -> None:
    from app.graph import routing

    pending = _clarification()
    fake = _RecordingLLM("COMPLETE")
    with patch("app.models.llm", new=fake):
        result = await routing.classify_intent(_state(reply, pending))

    assert fake.calls == 0, "no model call for a bare negative"
    assert result["intent"] == "CHAT"
    assert result["pending_clarification"] is None
    assert result.get("classification_error_fallback") is True
    outbound = result.get("pending_outbound", [])
    assert outbound, "must send an acknowledgement"
    assert outbound[0]["body"] == routing._CLARIFICATION_DECLINED_REPLY


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["no", "nope", "neither"])
async def test_negative_reply_without_live_clarification_goes_to_model(reply: str) -> None:
    from app.graph import routing

    fake = _RecordingLLM("CHAT")
    with patch("app.models.llm", new=fake):
        result = await routing.classify_intent(_state(reply))

    assert fake.calls == 1, "bare negative without live clarification goes to model"
    assert result["intent"] == "CHAT"
    assert result["pending_clarification"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pending",
    [
        _clarification(asked_at=datetime.now(UTC) - timedelta(hours=2)),
        _clarification(kind="something_else"),
        _clarification(attempts=0),
        _clarification(candidates="not-a-list"),
        _clarification(asked_at=None) | {"asked_at": "not-a-timestamp"},
    ],
    ids=["expired", "wrong-kind", "bad-attempts", "bad-candidates", "bad-timestamp"],
)
async def test_expired_or_malformed_clarification_disables_the_guard(pending: Any) -> None:
    from app.graph import routing

    fake = _RecordingLLM("ADD_TASK")
    with patch("app.models.llm", new=fake):
        result = await routing.classify_intent(_state("the first one", pending))

    assert fake.calls == 1
    assert result["intent"] == "ADD_TASK"
    assert result["pending_clarification"] is None


# ---------------------------------------------------------------------------
# unlisted_report: the question after a report that matched no open task
# ---------------------------------------------------------------------------

_TITLE = "Pay the placeholder bill"
_OPTION = {"page_id": "<page-id-1>", "title": "Test task one"}


def _unlisted(stage: str) -> dict[str, Any]:
    """The three shapes an unlisted-report question can take.

    - ``option``: "Did you mean <option>?" with a proposed title kept for a "no";
    - ``log``: "Want me to log '<title>' as done?" (title, no candidates);
    - ``no_title``: "Did you mean <option>?" with no title to fall back on.
    """
    if stage == "option":
        return _clarification(
            kind="unlisted_report", candidates=[dict(_OPTION)], title=_TITLE
        )
    if stage == "log":
        return _clarification(kind="unlisted_report", attempts=2, candidates=[], title=_TITLE)
    return _clarification(kind="unlisted_report", candidates=[dict(_OPTION)], title="")


def test_live_clarification_accepts_the_unlisted_report_kind() -> None:
    from app.graph import routing

    for stage in ("option", "log", "no_title"):
        pending = _unlisted(stage)
        assert routing._live_clarification(_state("x", pending)) == pending


@pytest.mark.parametrize(
    "pending",
    [
        _clarification(
            kind="unlisted_report", asked_at=datetime.now(UTC) - timedelta(hours=2), title=_TITLE
        ),
        _clarification(kind="unlisted_report", attempts=3, title=_TITLE),
        _clarification(kind="unlisted_report", title=7),
    ],
    ids=["expired", "past-attempt-cap", "non-string-title"],
)
def test_live_clarification_applies_the_same_rules_to_unlisted_reports(pending: Any) -> None:
    from app.graph import routing

    assert routing._live_clarification(_state("x", pending)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["option", "log", "no_title"])
@pytest.mark.parametrize("reply", ["yes", "yeah", "the first one", "that one"])
async def test_affirmative_or_positional_reply_to_an_unlisted_report_routes_complete(
    stage: str, reply: str
) -> None:
    from app.graph import routing

    pending = _unlisted(stage)
    fake = _RecordingLLM("ADD_TASK")
    with patch("app.models.llm", new=fake):
        result = await routing.classify_intent(_state(reply, pending))

    assert fake.calls == 0
    assert result["intent"] == "COMPLETE"
    assert result["pending_clarification"] == pending


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["no", "nope", "neither"])
async def test_no_to_the_named_option_offers_to_log_the_report(reply: str) -> None:
    """Declining the option is not declining the accomplishment."""
    from app.graph import routing

    fake = _RecordingLLM("COMPLETE")
    with patch("app.models.llm", new=fake):
        result = await routing.classify_intent(_state(reply, _unlisted("option")))

    assert fake.calls == 0
    assert result["intent"] == "CHAT"
    assert result["classification_error_fallback"] is True
    body = result["pending_outbound"][0]["body"]
    assert body == f"Got it. Want me to log '{_TITLE}' as done?"
    assert "notion_page_title" not in result["pending_outbound"][0]
    stage_two = result["pending_clarification"]
    assert stage_two["kind"] == "unlisted_report"
    assert stage_two["candidates"] == []
    assert stage_two["attempts"] == 2
    assert stage_two["title"] == _TITLE
    # The stage-two record is itself live, so its "yes" reaches complete_node.
    assert routing._live_clarification(_state("yes", stage_two)) == stage_two


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["log", "no_title"])
@pytest.mark.parametrize("reply", ["no", "nope"])
async def test_no_at_the_log_stage_or_without_a_title_leaves_it_open(
    stage: str, reply: str
) -> None:
    from app.graph import routing

    fake = _RecordingLLM("COMPLETE")
    with patch("app.models.llm", new=fake):
        result = await routing.classify_intent(_state(reply, _unlisted(stage)))

    assert fake.calls == 0
    assert result["intent"] == "CHAT"
    assert result["pending_clarification"] is None
    assert result["pending_outbound"][0]["body"] == routing._CLARIFICATION_DECLINED_REPLY


@pytest.mark.parametrize(
    ("reply", "affirmative", "selects"),
    [
        ("yes", True, True),
        ("Yeah!", True, True),
        ("the first one", False, True),
        ("that one", False, True),
        ("1", False, True),
        ("the second one", False, False),
        ("no", False, False),
        ("yes log it please", False, False),
    ],
)
def test_affirmative_and_single_option_readers(
    reply: str, affirmative: bool, selects: bool
) -> None:
    from app.graph import routing

    assert routing.is_affirmative_answer(reply) is affirmative
    assert routing.selects_single_option(reply) is selects
