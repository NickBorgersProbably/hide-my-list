"""Unit tests for the post-send interaction review (app/graph/interaction_review.py).

No database, no model: the verdict parser, the limit arithmetic, the settings,
the listener's pending-message check, the turn-action record, the prompt's
structure, and the exact call shapes of every side effect `review_turn` makes
inside its catch-all handler (validated against the real signatures, so a
renamed parameter fails here instead of being swallowed into a logged error).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from structlog.testing import capture_logs

from app.graph import interaction_review as review
from app.graph.interaction_review import (
    exceeds_alert_threshold,
    is_rate_limited,
    parse_verdict,
    review_settings,
)

OPEN = {"<page_open>", "<page_other>"}
DONE = {"<page_done>"}


def _json(**fields: Any) -> str:
    base: dict[str, Any] = {
        "verdict": "correct",
        "reason": "placeholder reason",
        "action": "complete_task",
        "page_id": "<page_open>",
        "title": None,
        "due": None,
        "follow_up_message": "{task} — marked that one done.",
    }
    base.update(fields)
    return json.dumps(base)


def _parse(text: str):
    return parse_verdict(text, open_page_ids=OPEN, completed_this_turn=DONE)


# ---------------------------------------------------------------------------
# parse_verdict — accepted
# ---------------------------------------------------------------------------


def test_ok_verdict_is_accepted() -> None:
    verdict = _parse(_json(verdict="ok", action="none", page_id=None, follow_up_message=""))
    assert verdict is not None
    assert (verdict.verdict, verdict.action, verdict.page_id) == ("ok", "none", None)


def test_complete_task_on_an_open_page_is_accepted() -> None:
    verdict = _parse(_json())
    assert verdict is not None
    assert (verdict.action, verdict.page_id) == ("complete_task", "<page_open>")
    assert "{task}" in verdict.follow_up_message


def test_code_fenced_verdict_is_accepted() -> None:
    assert _parse(f"```json\n{_json()}\n```") is not None


def test_create_task_is_accepted_with_title_and_due() -> None:
    verdict = _parse(_json(
        action="create_task", page_id=None, title="Renew the library card",
        due="2026-01-02T09:00:00+00:00", follow_up_message="Added {task} to your list.",
    ))
    assert verdict is not None
    assert (verdict.title, verdict.due) == ("Renew the library card", "2026-01-02T09:00:00+00:00")


def test_reopen_of_a_page_completed_this_turn_is_accepted() -> None:
    verdict = _parse(_json(
        action="reopen_task", page_id="<page_done>",
        follow_up_message="{task} is back on your list.",
    ))
    assert verdict is not None and verdict.action == "reopen_task"


def test_send_only_naming_a_listed_page_is_accepted() -> None:
    verdict = _parse(_json(action="send_only", follow_up_message="That was {task}."))
    assert verdict is not None and verdict.action == "send_only"


# ---------------------------------------------------------------------------
# parse_verdict — rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("not json at all", "not_json"),
        ("[1, 2]", "not_object"),
        (_json(confidence=0.9), "unknown_key"),
        (_json(verdict="maybe"), "unknown_verdict"),
        (_json(action="delete_task"), "unknown_action"),
        (_json(verdict="ok", action="complete_task"), "ok_with_action"),
        (_json(verdict="ok", action="none", page_id=None), "ok_with_action"),
        (_json(action="none", page_id=None), "correct_without_action"),
        (_json(follow_up_message="Marked that one done."), "follow_up_missing_task_token"),
        (_json(follow_up_message=""), "follow_up_missing_task_token"),
        (_json(follow_up_message="{task} " + "x" * 400), "follow_up_too_long"),
        (_json(page_id="<page_invented>"), "page_not_open"),
        (_json(page_id=None), "page_not_open"),
        (_json(page_id="<page_done>"), "page_not_open"),
        (
            _json(action="reopen_task", page_id="<page_open>",
                  follow_up_message="{task} is back on your list."),
            "page_not_completed_this_turn",
        ),
        (
            _json(action="reopen_task", page_id="<page_done>",
                  follow_up_message="{task} — nice work!"),
            "reopen_not_stated",
        ),
        (_json(action="send_only", page_id="<page_invented>"), "page_not_listed"),
        (_json(action="create_task", page_id="<page_open>", title="X"), "create_with_page_id"),
        (_json(action="create_task", page_id=None, title=None), "bad_title"),
        (_json(action="create_task", page_id=None, title="x" * 201), "bad_title"),
        (_json(action="create_task", page_id=None, title="two\nlines"), "bad_title"),
        (_json(action="create_task", page_id=None, title="X", due="next tuesday"), "bad_due"),
        (_json(page_id=7), "bad_type"),
        (_json(reason=["x"]), "bad_type"),
    ],
)
def test_invalid_verdicts_are_rejected_with_a_code(text: str, code: str) -> None:
    with capture_logs() as logs:
        assert _parse(text) is None
    rejected = [e for e in logs if e["event"] == "interaction_review.verdict_rejected"]
    assert [e["rejection"] for e in rejected] == [code]
    # Only the code is logged: never the reason, the follow-up, or a title.
    assert set(rejected[0]) <= {"event", "rejection", "log_level"}


@pytest.mark.parametrize(
    "follow_up",
    [
        "Looks like you forgot {task}, marked it done.",
        "You didn't mention {task}, so I marked it done.",
        "{task} wasn't on your list, so I added it.",
        "You missed {task} — done now.",
    ],
)
def test_blame_phrasing_is_rejected(follow_up: str) -> None:
    with capture_logs() as logs:
        assert _parse(_json(follow_up_message=follow_up)) is None
    assert [e["rejection"] for e in logs] == ["follow_up_blame"]


def test_blame_patterns_cover_the_shame_catalog() -> None:
    """Every phrase the tests score delivered text against is refused up front."""
    from tests.support.shame import BANNED_PATTERNS

    guarded = {pattern.pattern for pattern in review._BLAME_PATTERNS}
    missing = [p.pattern for p in BANNED_PATTERNS if p.pattern not in guarded]
    assert not missing


# ---------------------------------------------------------------------------
# Limits and settings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "limit", "limited"),
    [(0, 3, False), (2, 3, False), (3, 3, True), (4, 3, True), (0, 0, True)],
)
def test_rate_limit_arithmetic(count: int, limit: int, limited: bool) -> None:
    assert is_rate_limited(count, limit) is limited


@pytest.mark.parametrize(
    ("count", "threshold", "alert"), [(4, 5, False), (5, 5, False), (6, 5, True)]
)
def test_alert_threshold_arithmetic(count: int, threshold: int, alert: bool) -> None:
    assert exceeds_alert_threshold(count, threshold) is alert


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "INTERACTION_REVIEW_ENABLED",
        "INTERACTION_REVIEW_DELAY_SECONDS",
        "INTERACTION_REVIEW_MAX_PER_HOUR",
        "INTERACTION_REVIEW_ALERT_THRESHOLD",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = review_settings()
    assert (settings.enabled, settings.delay_seconds, settings.max_per_hour,
            settings.alert_threshold) == (True, 3.0, 3, 5)


def test_settings_read_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERACTION_REVIEW_ENABLED", "false")
    monkeypatch.setenv("INTERACTION_REVIEW_DELAY_SECONDS", "0.5")
    monkeypatch.setenv("INTERACTION_REVIEW_MAX_PER_HOUR", "1")
    monkeypatch.setenv("INTERACTION_REVIEW_ALERT_THRESHOLD", "not-a-number")
    settings = review_settings()
    assert (settings.enabled, settings.delay_seconds, settings.max_per_hour,
            settings.alert_threshold) == (False, 0.5, 1, 5)


# ---------------------------------------------------------------------------
# Listener: pending-message check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buffer_has_pending_is_per_peer() -> None:
    from app.ingress.signal_listener import _InboundMessageBuffer, _QueuedMessage

    buffer = _InboundMessageBuffer(4)
    assert buffer.has_pending("<peer_a>") is False
    await buffer.try_add(_QueuedMessage(peer="<peer_a>", text="Test message"))
    assert buffer.has_pending("<peer_a>") is True
    assert buffer.has_pending("<peer_b>") is False
    await buffer.get()
    assert buffer.has_pending("<peer_a>") is False


# ---------------------------------------------------------------------------
# Turn actions and inputs
# ---------------------------------------------------------------------------


def test_record_turn_action_appends_without_mutating() -> None:
    from app.graph.context import TURN_ACTION_CAP, record_turn_action

    first = record_turn_action(None, action="clarify")
    second = record_turn_action(
        first, action="notion.update_status", page_id="<page_A>", status="Completed"
    )
    assert first == [{"action": "clarify", "page_id": "", "status": ""}]
    assert second[-1] == {
        "action": "notion.update_status", "page_id": "<page_A>", "status": "Completed",
    }
    capped: list[Any] = []
    for _ in range(TURN_ACTION_CAP + 3):
        capped = record_turn_action(capped, action="suggest", page_id="<page_A>")
    assert len(capped) == TURN_ACTION_CAP


def test_inputs_carry_the_delivered_reply_and_this_turns_completions() -> None:
    now = datetime.now(UTC)
    state = {
        "incoming": "done with the plants",
        "intent": "COMPLETE",
        "messages": [
            HumanMessage(content="done with the plants"),
            AIMessage(content="Water the plants — done. 🎉"),
        ],
        "recent_tasks": [{
            "page_id": "<page_done>", "title": "Water the plants", "kind": "task",
            "event": "completed", "at": now.isoformat(),
        }],
        "turn_actions": [
            {"action": "notion.update_status", "page_id": "<page_done>", "status": "Completed"},
            {"action": "reward", "page_id": "<page_done>", "status": ""},
        ],
    }
    open_list = [{"id": "<page_open>", "title": "Sort the mail", "kind": "task"}]
    inputs = review.inputs_from_state(state, open_list, now=now)
    assert inputs.reply == "Water the plants — done. 🎉"
    assert inputs.intent == "COMPLETE"
    assert inputs.completed_this_turn == ({"id": "<page_done>", "title": "Water the plants"},)
    assert "notion.update_status Completed page <page_done>" in inputs.turn_actions
    assert inputs.open_tasks == ({"id": "<page_open>", "title": "Sort the mail", "kind": "task"},)


def test_completed_this_turn_reads_both_completion_forms() -> None:
    """A status write to Completed and a page created Completed both count.

    COMPLETE writes the status; logging finished work (COMPLETE's unlisted
    report, intake's already-finished save) creates the page Completed, or
    writes the status when it matches an open task.
    """
    actions = [
        {"action": "notion.update_status", "page_id": "<page_A>", "status": "Completed"},
        {"action": "notion.create_task", "page_id": "<page_B>", "status": "Completed"},
        {"action": "notion.create_task", "page_id": "<page_C>", "status": ""},
        {"action": "notion.update_status", "page_id": "<page_D>", "status": "In Progress"},
        {"action": "reward", "page_id": "<page_A>", "status": ""},
    ]
    ledger = [{
        "page_id": "<page_B>", "title": "Pay the placeholder bill", "kind": "task",
        "event": "completed", "at": datetime.now(UTC).isoformat(),
    }]
    assert review.completed_this_turn(actions, ledger) == [
        {"id": "<page_A>", "title": ""},
        {"id": "<page_B>", "title": "Pay the placeholder bill"},
    ]


def test_prompt_renders_every_input_and_section() -> None:
    now = datetime.now(UTC)
    inputs = review.inputs_from_state(
        {"incoming": "Done!", "intent": "COMPLETE", "messages": []},
        [{"id": "<page_open>", "title": "Sort the mail", "kind": "reminder"}],
        now=now,
    )
    rendered = review._render_prompt(inputs)
    for anchor in (
        "## Interaction Review", "### Inputs", "### Verdict Schema",
        "### Correction Policy", "### Shame Prevention", "{task}",
        "<page_open>", '"Done!"', "complete_task", "reopen_task", "send_only",
    ):
        assert anchor in rendered, anchor


# ---------------------------------------------------------------------------
# review_turn — side-effect call shapes (bug class 10)
# ---------------------------------------------------------------------------


def _page(page_id: str, title: str, *, reminder: bool = False) -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Status": {"select": {"name": "Pending"}},
            "Is Reminder": {"checkbox": reminder},
        },
    }


_REVIEW_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


class _Graph:
    def __init__(self) -> None:
        self.aupdate_state = AsyncMock()

    async def aget_state(self, config: Any) -> Any:
        return SimpleNamespace(config={"configurable": {"checkpoint_id": "<ckpt>"}})


def _bind(real: Any, call: Any) -> dict[str, Any]:
    return dict(inspect.signature(real).bind(*call.args, **call.kwargs).arguments)


async def _run(
    verdict_json: str, *, final_state: dict[str, Any], update_status_error: Exception | None = None
) -> dict[str, Any]:
    """Run review_turn with every dependency mocked; return the mocks."""
    from app.tools import interaction_reviews, notion, ops_alerts, reminders, signal_client
    from app.tools import rewards as rewards_module

    mocks: dict[str, Any] = {
        "real": {
            "update_status": notion.update_status,
            "create_task": notion.create_task,
            "finalize": interaction_reviews.finalize,
            "count": interaction_reviews.count_executed_corrections,
            "send_message": signal_client.send_message,
            "maybe_reward": rewards_module.maybe_reward,
            "cancel": reminders.cancel_pending_reminders,
            "resolve": reminders.resolve_recent_outbound,
            "enqueue": ops_alerts.enqueue,
        },
        "update_status": AsyncMock(return_value={}, side_effect=update_status_error),
        "create_task": AsyncMock(return_value={"id": "<page_new>"}),
        "finalize": AsyncMock(return_value=True),
        # First call: the hour's count (under the limit); second: the day's
        # count (over the alert threshold).
        "count": AsyncMock(side_effect=[0, 9]),
        "send_message": AsyncMock(return_value={"timestamp": 1}),
        "maybe_reward": AsyncMock(return_value={"text": "🎉", "attachment_path": None}),
        "cancel": AsyncMock(return_value=1),
        "resolve": AsyncMock(return_value=0),
        "enqueue": AsyncMock(),
        "graph": _Graph(),
    }
    query_all = AsyncMock(return_value={"results": [
        _page("<page_open>", "Renew the library card", reminder=True),
        _page("<page_other>", "Sort the mail"),
    ]})
    with (
        patch("app.tools.notion.query_all", query_all),
        patch("app.tools.notion.update_status", mocks["update_status"]),
        patch("app.tools.notion.create_task", mocks["create_task"]),
        patch("app.tools.interaction_reviews.finalize", mocks["finalize"]),
        patch("app.tools.interaction_reviews.count_executed_corrections", mocks["count"]),
        patch("app.tools.signal_client.send_message", mocks["send_message"]),
        patch("app.tools.rewards.maybe_reward", mocks["maybe_reward"]),
        patch("app.tools.reminders.cancel_pending_reminders", mocks["cancel"]),
        patch("app.tools.reminders.resolve_recent_outbound", mocks["resolve"]),
        patch("app.tools.ops_alerts.enqueue", mocks["enqueue"]),
        patch.object(review, "judge_turn", AsyncMock(return_value=verdict_json)),
        capture_logs() as logs,
    ):
        await review.review_turn(
            peer="<recipient>",
            final_state=final_state,
            graph=mocks["graph"],
            config={"configurable": {"thread_id": "<recipient>"}},
            review_id=_REVIEW_ID,
            turn_ref="<ckpt>",
        )
    mocks["logs"] = logs
    return mocks


_STATE: dict[str, Any] = {
    "incoming": "Done!",
    "intent": "COMPLETE",
    "messages": [HumanMessage(content="Done!"), AIMessage(content="Which task did you mean?")],
    "recent_tasks": [],
    "turn_actions": [{"action": "clarify", "page_id": "", "status": ""}],
    "streak": 2,
    "tasks_completed_today": 1,
    "pending_clarification": {"kind": "complete_target", "attempts": 1, "candidates": []},
}


@pytest.mark.asyncio
async def test_complete_task_call_shapes_match_real_signatures() -> None:
    mocks = await _run(_json(), final_state=_STATE)
    real = mocks["real"]

    assert _bind(real["update_status"], mocks["update_status"].await_args) == {
        "page_id": "<page_open>", "new_status": "Completed",
    }
    assert _bind(real["cancel"], mocks["cancel"].await_args) == {
        "peer": "<recipient>", "notion_page_id": "<page_open>",
    }
    assert _bind(real["resolve"], mocks["resolve"].await_args) == {
        "peer": "<recipient>", "signal_timestamp": 0, "notion_page_id": "<page_open>",
    }
    assert _bind(real["maybe_reward"], mocks["maybe_reward"].await_args) == {
        "peer": "<recipient>", "task_title": "Renew the library card",
        "notion_page_id": "<page_open>", "streak": 3,
    }
    sent = _bind(real["send_message"], mocks["send_message"].await_args)
    assert sent["recipient"] == "<recipient>"
    assert sent["message"] == "Renew the library card — marked that one done. 🎉"
    assert isinstance(sent["idempotency_key"], str) and len(sent["idempotency_key"]) == 32
    assert "attachment_paths" not in sent

    stored = _bind(real["finalize"], mocks["finalize"].await_args)
    assert stored == {
        "review_id": _REVIEW_ID, "verdict": "correct", "reason": "placeholder reason",
        "action": "complete_task", "action_page_id": "<page_open>", "executed": True,
        "follow_up_sent": True,
    }
    # Finalized once, after the checkpoint write.
    mocks["finalize"].assert_awaited_once()
    assert [_bind(real["count"], c) for c in mocks["count"].await_args_list] == [
        {"peer": "<recipient>", "window_seconds": 3600.0},
        {"peer": None, "window_seconds": 86400.0},
    ]
    alert = _bind(real["enqueue"], mocks["enqueue"].await_args)
    assert alert["kind"] == "interaction_review_excess"
    assert alert["severity"] == "warning"
    assert "<recipient>" not in alert["body"] and "library" not in alert["body"]

    update = mocks["graph"].aupdate_state.await_args
    from langgraph.pregel import Pregel

    bound = inspect.signature(Pregel.aupdate_state).bind(None, *update.args, **update.kwargs)
    assert bound.arguments["as_node"] == "send"
    values = bound.arguments["values"]
    assert set(values) == {
        "recent_tasks", "streak", "tasks_completed_today", "conversation_state",
        "pending_clarification", "messages",
    }
    assert values["pending_clarification"] is None
    assert values["streak"] == 3
    assert values["recent_tasks"][0]["event"] == "completed"
    assert values["recent_tasks"][0]["kind"] == "reminder"
    assert values["messages"][0].content == sent["message"]

    events = [e["event"] for e in mocks["logs"]]
    assert "interaction_review.corrected" in events
    for entry in mocks["logs"]:
        flat = json.dumps(entry, default=str)
        assert "library" not in flat.lower() and "<recipient>" not in flat


@pytest.mark.asyncio
async def test_create_task_call_shape_matches_real_signature() -> None:
    mocks = await _run(
        _json(action="create_task", page_id=None, title="Renew the passport",
              due="2026-02-01T09:00:00+00:00", follow_up_message="Added {task} to your list."),
        final_state=_STATE,
    )
    assert _bind(mocks["real"]["create_task"], mocks["create_task"].await_args) == {
        "title": "Renew the passport", "work_type": "focus",
        "due_at_iso": "2026-02-01T09:00:00+00:00",
    }
    sent = _bind(mocks["real"]["send_message"], mocks["send_message"].await_args)
    assert sent["message"] == "Added Renew the passport to your list."
    values = mocks["graph"].aupdate_state.await_args.args[1]
    assert values["recent_tasks"][0]["page_id"] == "<page_new>"
    assert values["recent_tasks"][0]["event"] == "added"


@pytest.mark.asyncio
async def test_reopen_task_writes_pending() -> None:
    state = {
        **_STATE,
        "turn_actions": [
            {"action": "notion.update_status", "page_id": "<page_done>", "status": "Completed"},
        ],
        "recent_tasks": [{
            "page_id": "<page_done>", "title": "Water the plants", "kind": "task",
            "event": "completed", "at": datetime.now(UTC).isoformat(),
        }],
    }
    mocks = await _run(
        _json(action="reopen_task", page_id="<page_done>",
              follow_up_message="{task} is back on your list."),
        final_state=state,
    )
    assert _bind(mocks["real"]["update_status"], mocks["update_status"].await_args) == {
        "page_id": "<page_done>", "new_status": "Pending",
    }
    sent = _bind(mocks["real"]["send_message"], mocks["send_message"].await_args)
    assert sent["message"] == "Water the plants is back on your list."


@pytest.mark.asyncio
async def test_ok_verdict_writes_nothing_but_the_row() -> None:
    mocks = await _run(
        _json(verdict="ok", action="none", page_id=None, follow_up_message=""),
        final_state=_STATE,
    )
    for name in ("update_status", "create_task", "send_message", "maybe_reward", "enqueue"):
        mocks[name].assert_not_awaited()
    mocks["graph"].aupdate_state.assert_not_awaited()
    stored = mocks["finalize"].await_args.kwargs
    assert (stored["verdict"], stored["action"], stored["executed"]) == ("ok", "none", False)


@pytest.mark.asyncio
async def test_invalid_verdict_is_stored_as_error_without_acting() -> None:
    mocks = await _run("Sure! I think the user meant the library card.", final_state=_STATE)
    mocks["update_status"].assert_not_awaited()
    mocks["send_message"].assert_not_awaited()
    stored = mocks["finalize"].await_args.kwargs
    assert (stored["verdict"], stored["reason"], stored["action"]) == (
        "error", "invalid_verdict", None,
    )


@pytest.mark.asyncio
async def test_a_failure_is_logged_and_stored_never_raised() -> None:
    mocks = await _run(_json(), final_state=_STATE, update_status_error=RuntimeError("down"))
    errors = [e for e in mocks["logs"] if e["event"] == "interaction_review.error"]
    assert [e["error_type"] for e in errors] == ["RuntimeError"]
    stored = mocks["finalize"].await_args.kwargs
    assert (stored["verdict"], stored["reason"], stored["executed"]) == (
        "error", "RuntimeError", False,
    )
    mocks["send_message"].assert_not_awaited()


# ---------------------------------------------------------------------------
# Job lifecycle: skip reasons, cancellation, stale checkpoint, pending row
# ---------------------------------------------------------------------------


def test_skip_reasons_match_the_literal_and_the_spec() -> None:
    from pathlib import Path
    from typing import get_args

    assert set(review.SKIP_REASONS) == set(get_args(review.SkipReason))
    assert set(review.SKIP_REASONS) == {
        "buffer_non_empty", "superseded", "cancelled", "rate_limited", "disabled", "timeout",
    }
    spec = (
        Path(__file__).resolve().parents[2] / "docs" / "ai-prompts" / "interaction-review.md"
    ).read_text()
    for reason in review.SKIP_REASONS + ("invalid_verdict", "stale_checkpoint"):
        assert f"`{reason}`" in spec, reason


def _lifecycle_patches(
    finalize: AsyncMock, *, judge: Any, update_status: Any, maybe_reward: Any
) -> Any:
    from contextlib import ExitStack

    stack = ExitStack()
    query_all = AsyncMock(return_value={"results": [
        _page("<page_open>", "Renew the library card"),
    ]})
    for target, value in (
        ("app.tools.notion.query_all", query_all),
        ("app.tools.notion.update_status", update_status),
        ("app.tools.interaction_reviews.finalize", finalize),
        ("app.tools.interaction_reviews.count_executed_corrections", AsyncMock(return_value=0)),
        ("app.tools.signal_client.send_message", AsyncMock(return_value={"timestamp": 1})),
        ("app.tools.rewards.maybe_reward", maybe_reward),
        ("app.tools.reminders.cancel_pending_reminders", AsyncMock(return_value=0)),
        ("app.tools.reminders.resolve_recent_outbound", AsyncMock(return_value=0)),
        ("app.tools.ops_alerts.enqueue", AsyncMock()),
    ):
        stack.enter_context(patch(target, value))
    stack.enter_context(patch.object(review, "judge_turn", judge))
    return stack


@pytest.mark.asyncio
@pytest.mark.parametrize(("message", "reason"), [
    ("timeout", "timeout"),
    ("cancelled", "cancelled"),
    (None, "cancelled"),
    ("not-a-reason", "cancelled"),
])
async def test_cancellation_finalizes_skipped_with_the_cancel_message(
    message: str | None, reason: str
) -> None:
    from app.tools import interaction_reviews

    finalize = AsyncMock(return_value=True)
    started = asyncio.Event()

    async def hanging_judge(_inputs: Any) -> str:
        started.set()
        await asyncio.Event().wait()
        return ""

    graph = _Graph()
    with _lifecycle_patches(
        finalize, judge=hanging_judge, update_status=AsyncMock(),
        maybe_reward=AsyncMock(),
    ), capture_logs() as logs:
        task = asyncio.create_task(review.review_turn(
            peer="<recipient>", final_state=_STATE, graph=graph,
            config={"configurable": {"thread_id": "<recipient>"}},
            review_id=_REVIEW_ID, turn_ref="<ckpt>",
        ))
        await started.wait()
        task.cancel(msg=message)
        with pytest.raises(asyncio.CancelledError):
            await task

    stored = _bind(interaction_reviews.finalize, finalize.await_args)
    assert stored == {
        "review_id": _REVIEW_ID, "verdict": "skipped", "reason": reason, "action": None,
        "action_page_id": None, "executed": False, "follow_up_sent": False,
    }
    graph.aupdate_state.assert_not_awaited()
    assert [e["reason"] for e in logs if e["event"] == "interaction_review.skipped"] == [reason]


@pytest.mark.asyncio
async def test_cancel_after_the_notion_write_records_it() -> None:
    finalize = AsyncMock(return_value=True)
    rewarding = asyncio.Event()

    async def hanging_reward(**_kwargs: Any) -> Any:
        rewarding.set()
        await asyncio.Event().wait()

    graph = _Graph()
    with _lifecycle_patches(
        finalize, judge=AsyncMock(return_value=_json()), update_status=AsyncMock(),
        maybe_reward=hanging_reward,
    ):
        task = asyncio.create_task(review.review_turn(
            peer="<recipient>", final_state=_STATE, graph=graph,
            config={"configurable": {"thread_id": "<recipient>"}},
            review_id=_REVIEW_ID, turn_ref="<ckpt>",
        ))
        await rewarding.wait()
        task.cancel(msg="timeout")
        with pytest.raises(asyncio.CancelledError):
            await task

    stored = finalize.await_args.kwargs
    assert (stored["verdict"], stored["reason"], stored["action"], stored["action_page_id"],
            stored["executed"], stored["follow_up_sent"]) == (
        "skipped", "timeout", "complete_task", "<page_open>", True, False,
    )
    graph.aupdate_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_moved_checkpoint_blocks_the_write() -> None:
    finalize = AsyncMock(return_value=True)
    graph = _Graph()

    async def moved(_config: Any) -> Any:
        return SimpleNamespace(config={"configurable": {"checkpoint_id": "<ckpt-newer>"}})

    graph.aget_state = moved  # type: ignore[method-assign]
    with _lifecycle_patches(
        finalize, judge=AsyncMock(return_value=_json()), update_status=AsyncMock(),
        maybe_reward=AsyncMock(return_value={"text": "", "attachment_path": None}),
    ), capture_logs() as logs:
        await review.review_turn(
            peer="<recipient>", final_state=_STATE, graph=graph,
            config={"configurable": {"thread_id": "<recipient>"}},
            review_id=_REVIEW_ID, turn_ref="<ckpt>",
        )

    graph.aupdate_state.assert_not_awaited()
    stored = finalize.await_args.kwargs
    assert (stored["verdict"], stored["reason"], stored["executed"], stored["follow_up_sent"]) == (
        "error", "stale_checkpoint", True, False,
    )
    assert "interaction_review.stale_checkpoint" in [e["event"] for e in logs]


@pytest.mark.asyncio
async def test_finalize_skipped_logs_only_the_first_finalize_and_never_raises() -> None:
    with patch(
        "app.tools.interaction_reviews.finalize", AsyncMock(side_effect=[True, False])
    ), capture_logs() as logs:
        await review.finalize_skipped(_REVIEW_ID, reason="cancelled")
        await review.finalize_skipped(_REVIEW_ID, reason="cancelled")
        await review.finalize_skipped(None, reason="cancelled")
    assert [e["event"] for e in logs] == ["interaction_review.skipped"]

    with patch(
        "app.tools.interaction_reviews.finalize", AsyncMock(side_effect=RuntimeError("down"))
    ), capture_logs() as logs:
        await review.finalize_skipped(_REVIEW_ID, reason="timeout")
    assert [(e["event"], e["error_type"]) for e in logs] == [
        ("interaction_review.store_failed", "RuntimeError"),
    ]


@pytest.mark.asyncio
async def test_listener_stores_the_pending_row_before_the_review_runs() -> None:
    from app.ingress.signal_listener import SignalListener
    from app.tools import interaction_reviews

    create = AsyncMock(return_value=_REVIEW_ID)
    review_turn = AsyncMock()
    listener = SignalListener(
        graph=_Graph(), authorized_peers=frozenset({"<recipient>"}),
        interaction_review_enabled=True, interaction_review_delay_seconds=0,
    )
    with (
        patch("app.tools.interaction_reviews.create_pending", create),
        patch.object(review, "review_turn", review_turn),
    ):
        listener._start_review(
            graph=listener._graph, peer="<recipient>", final_state=_STATE,
            config={"configurable": {"thread_id": "<recipient>"}},
        )
        await listener.wait_for_review("<recipient>")

    assert _bind(interaction_reviews.create_pending, create.await_args) == {
        "peer": "<recipient>", "turn_ref": "<ckpt>", "intent": "COMPLETE",
    }
    kwargs = review_turn.await_args.kwargs
    assert (kwargs["review_id"], kwargs["turn_ref"]) == (_REVIEW_ID, "<ckpt>")


@pytest.mark.asyncio
async def test_listener_runs_no_review_when_the_pending_row_cannot_be_stored() -> None:
    from app.ingress.signal_listener import SignalListener

    review_turn = AsyncMock()
    listener = SignalListener(
        graph=_Graph(), authorized_peers=frozenset({"<recipient>"}),
        interaction_review_enabled=True, interaction_review_delay_seconds=0,
    )
    with (
        patch("app.tools.interaction_reviews.create_pending",
              AsyncMock(side_effect=RuntimeError("down"))),
        patch.object(review, "review_turn", review_turn),
        capture_logs() as logs,
    ):
        listener._start_review(
            graph=listener._graph, peer="<recipient>", final_state=_STATE,
            config={"configurable": {"thread_id": "<recipient>"}},
        )
        await listener.wait_for_review("<recipient>")

    review_turn.assert_not_awaited()
    assert [(e["event"], e["error_type"]) for e in logs
            if e["event"] == "interaction_review.pending_store_failed"] == [
        ("interaction_review.pending_store_failed", "RuntimeError"),
    ]
