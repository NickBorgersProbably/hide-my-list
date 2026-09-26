"""Post-send interaction review.

A turn is judged fast: one classifier call, one node, one reply. Some turns go
wrong in ways only the whole exchange shows — "Done!" a minute after adding a
reminder that the node could not place, a request to track something that
saved nothing. This module re-reads a finished turn with the medium model and
may repair one thing and send one short follow-up.

It is not a graph node. `SignalListener` stores a pending job row and schedules
`review_turn` as a background task after `graph.ainvoke` returns, so the review
never delays the reply. It yields to live conversation: before it acts it is
skipped or cancelled by the peer's next message; once it acts, the next turn
waits for it up to a bound and then cancels it (see
`docs/ai-prompts/interaction-review.md`). The spec and the prompt
(`app/prompts/interaction_review.md.j2`) define the verdict contract;
`parse_verdict` enforces it before anything is written.

Guardrails, all deterministic:
- one action per turn, `complete_task` or `send_only`; the review never
  creates a task or a reminder and never reopens one. Page ids must come from
  the lists the model was shown (`complete_task`: open tasks; `send_only`:
  open tasks or this turn's completions);
- the model writes no user-facing text: the follow-up is a fixed template per
  action, rendered through `render_task_token` with the stored title;
- the row is claimed (`executing`, with its action and page) before the first
  external effect, and each effect is recorded as it lands, so a restart never
  replays a claimed job;
- the follow-up is sent and the checkpoint written only while the thread's
  latest checkpoint is still the reviewed turn's (`turn_ref`), so a review
  never speaks about or overwrites a newer turn;
- executed corrections are rate limited per peer per hour and raise an ops
  alert past a 24-hour threshold; every exit path, cancellation included,
  finalizes the job's `interaction_reviews` row (via
  `app/tools/interaction_reviews.py`; this module issues no SQL).

Privacy: messages, titles, and the model's `reason` are the user's private
data. They go into the prompt and the verdict table, never into logs — log
enum values, ids, counts, and booleans only.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

import structlog

from app.graph.context import (
    ledger_entry,
    record_task_event,
    render_history,
    render_recent_tasks,
)
from app.graph.nodes._task_token import TASK_TOKEN, render_task_token

log = structlog.get_logger(__name__)

VerdictKind = Literal["ok", "correct"]
ReviewAction = Literal["none", "complete_task", "send_only"]

ACTIONS: tuple[str, ...] = ("none", "complete_task", "send_only")
_VERDICTS: frozenset[str] = frozenset({"ok", "correct"})
_KEYS: frozenset[str] = frozenset({"verdict", "reason", "action", "page_id"})

_HOUR_SECONDS = 3600.0
_DAY_SECONDS = 86400.0

_DEFAULT_DELAY_SECONDS = 3.0
_DEFAULT_MAX_PER_HOUR = 3
_DEFAULT_ALERT_THRESHOLD = 5

# Why a review ended without a verdict of its own. The listener cancels a
# review with one of these as the cancellation message.
SkipReason = Literal[
    "buffer_non_empty", "superseded", "cancelled", "rate_limited", "disabled", "timeout"
]
SKIP_REASONS: tuple[str, ...] = (
    "buffer_non_empty", "superseded", "cancelled", "rate_limited", "disabled", "timeout",
)

# The only text a review ever sends. The model writes none of it: `{task}`
# is filled with the stored title by `render_task_token`.
FOLLOW_UP_TEMPLATES: dict[str, str] = {
    "complete_task": "{task} — marked that one done.",
    "send_only": "That was {task}.",
}

# Reward sizing defaults, the same ones `maybe_reward` applies when a page
# carries no metadata.
_DEFAULT_TIME_ESTIMATE = 30


@dataclass(frozen=True)
class Verdict:
    """A validated review verdict."""

    verdict: VerdictKind
    reason: str
    action: ReviewAction
    page_id: str | None


@dataclass(frozen=True)
class ReviewSettings:
    """Environment-driven review configuration."""

    enabled: bool
    delay_seconds: float
    max_per_hour: int
    alert_threshold: int


@dataclass(frozen=True)
class ReviewInputs:
    """Everything the review prompt reads, already rendered or listed."""

    user_message: str
    reply: str
    intent: str
    history: str
    recent_tasks: str
    turn_actions: str
    open_tasks: tuple[Mapping[str, str], ...]
    completed_this_turn: tuple[Mapping[str, str], ...]
    current_time: str


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value >= 0 else default


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value >= 0 else default


def review_settings() -> ReviewSettings:
    """Read the review settings from the environment, falling back to defaults.

    `INTERACTION_REVIEW_ENABLED` is on unless set to false/0/no/off. Unparseable
    or negative numbers fall back to the defaults.
    """
    enabled_raw = os.environ.get("INTERACTION_REVIEW_ENABLED", "true").strip().lower()
    return ReviewSettings(
        enabled=enabled_raw not in ("false", "0", "no", "off"),
        delay_seconds=_env_float("INTERACTION_REVIEW_DELAY_SECONDS", _DEFAULT_DELAY_SECONDS),
        max_per_hour=_env_int("INTERACTION_REVIEW_MAX_PER_HOUR", _DEFAULT_MAX_PER_HOUR),
        alert_threshold=_env_int(
            "INTERACTION_REVIEW_ALERT_THRESHOLD", _DEFAULT_ALERT_THRESHOLD
        ),
    )


def is_rate_limited(executed_last_hour: int, max_per_hour: int) -> bool:
    """Whether a peer has used up its executed corrections for the hour."""
    return executed_last_hour >= max_per_hour


def exceeds_alert_threshold(executed_last_day: int, threshold: int) -> bool:
    """Whether executed corrections in 24 hours warrant an ops alert."""
    return executed_last_day > threshold


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def completed_this_turn(
    turn_actions: Sequence[object] | None, recent_tasks: Sequence[object] | None
) -> list[dict[str, str]]:
    """Pages this turn wrote Completed, as `{id, title}` (title from the ledger).

    Both forms count: `notion.update_status` to Completed (COMPLETE, and a
    logged accomplishment that matched an open task) and `notion.create_task`
    created Completed (a logged accomplishment with no matching task, from
    COMPLETE or from intake's already-finished path).
    """
    completed: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in turn_actions or []:
        if not isinstance(raw, Mapping):
            continue
        page_id = str(raw.get("page_id") or "")
        if (
            raw.get("action") not in ("notion.update_status", "notion.create_task")
            or raw.get("status") != "Completed"
            or not page_id
            or page_id in seen
        ):
            continue
        seen.add(page_id)
        known = ledger_entry(recent_tasks, page_id)
        completed.append({"id": page_id, "title": known["title"] if known else ""})
    return completed


def _render_turn_actions(turn_actions: Sequence[object] | None) -> str:
    lines: list[str] = []
    for raw in turn_actions or []:
        if not isinstance(raw, Mapping):
            continue
        action = str(raw.get("action") or "")
        if not action:
            continue
        parts = [f"- {action}"]
        if raw.get("status"):
            parts.append(str(raw["status"]))
        if raw.get("page_id"):
            parts.append(f"page {raw['page_id']}")
        lines.append(" ".join(parts))
    return "\n".join(lines) if lines else "Nothing was written or offered."


def _last_reply(messages: Sequence[Any] | None) -> str:
    for message in reversed(list(messages or [])):
        if str(getattr(message, "type", "")) == "ai":
            return " ".join(str(getattr(message, "content", "")).split())
    return ""


def inputs_from_state(
    state: Mapping[str, Any],
    open_list: Sequence[Mapping[str, str]],
    *,
    now: datetime,
) -> ReviewInputs:
    """Build the review inputs from a finished turn's state and the open tasks."""
    messages = state.get("messages") or []
    recent = state.get("recent_tasks") or []
    actions = state.get("turn_actions") or []
    return ReviewInputs(
        user_message=str(state.get("incoming") or ""),
        reply=_last_reply(messages),
        intent=str(state.get("intent") or "unknown"),
        history=render_history(messages),
        recent_tasks=render_recent_tasks(recent, now=now),
        turn_actions=_render_turn_actions(actions),
        open_tasks=tuple(
            {"id": task["id"], "title": task["title"], "kind": task.get("kind", "task")}
            for task in open_list
        ),
        completed_this_turn=tuple(completed_this_turn(actions, recent)),
        current_time=now.astimezone(UTC).isoformat(timespec="minutes"),
    )


def _render_prompt(inputs: ReviewInputs) -> str:
    from app.prompts.loader import render

    return render(
        "interaction_review.md.j2",
        {
            "current_time": inputs.current_time,
            "intent": inputs.intent,
            "user_message": inputs.user_message,
            "reply": inputs.reply,
            "history": inputs.history,
            "recent_tasks": inputs.recent_tasks,
            "turn_actions": inputs.turn_actions,
            "open_tasks_json": json.dumps(list(inputs.open_tasks), ensure_ascii=False),
            "completed_json": json.dumps(list(inputs.completed_this_turn), ensure_ascii=False),
        },
    )


async def judge_turn(inputs: ReviewInputs) -> str:
    """Render the review prompt and return the model's raw text.

    Pure prompt + model call: no parsing, no side effects, no exception
    handling, so the eval runner scores exactly what production would parse.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from app.models import llm

    model = llm("medium", caller="interaction_review")
    response = await model.ainvoke([
        SystemMessage(content=_render_prompt(inputs)),
        HumanMessage(content="Return only the JSON object."),
    ])
    return str(response.content)


# ---------------------------------------------------------------------------
# Verdict parsing
# ---------------------------------------------------------------------------


def _reject(code: str) -> None:
    log.info("interaction_review.verdict_rejected", rejection=code)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    raise TypeError


def parse_verdict(
    text: str,
    *,
    open_page_ids: Sequence[str] | set[str] | frozenset[str],
    completed_this_turn: Sequence[str] | set[str] | frozenset[str],
) -> Verdict | None:
    """Validate the model's verdict; None (logged) for anything not to act on.

    The text must hold exactly one JSON object (an optional code fence around
    it is tolerated) with only the schema's keys. See the Verdict Schema
    section of `docs/ai-prompts/interaction-review.md` for every rule.
    """
    stripped = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL)
    if fenced:
        stripped = fenced.group(1)
    try:
        loaded = json.loads(stripped)
    except json.JSONDecodeError:
        _reject("not_json")
        return None
    if not isinstance(loaded, dict):
        _reject("not_object")
        return None
    if set(loaded) - _KEYS:
        _reject("unknown_key")
        return None
    if _KEYS - set(loaded):
        _reject("missing_key")
        return None

    verdict = loaded.get("verdict")
    action = loaded.get("action")
    if verdict not in _VERDICTS:
        _reject("unknown_verdict")
        return None
    if action not in ACTIONS:
        _reject("unknown_action")
        return None
    reason = loaded.get("reason", "")
    if not isinstance(reason, str):
        _reject("bad_type")
        return None
    try:
        page_id = _optional_str(loaded.get("page_id"))
    except TypeError:
        _reject("bad_type")
        return None

    if verdict == "ok":
        if action != "none" or page_id:
            _reject("ok_with_action")
            return None
        return Verdict("ok", reason.strip(), "none", None)

    if action == "none":
        _reject("correct_without_action")
        return None
    open_ids = set(open_page_ids)
    completed_ids = set(completed_this_turn)
    if action == "complete_task":
        if not page_id or page_id not in open_ids:
            _reject("page_not_open")
            return None
    elif not page_id or page_id not in open_ids | completed_ids:  # send_only
        _reject("page_not_listed")
        return None

    return Verdict("correct", reason.strip(), cast(ReviewAction, action), page_id)


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Execution:
    """What a correction did, for the follow-up and the checkpoint write."""

    page_id: str
    title: str
    updates: dict[str, Any]
    reward_text: str = ""
    attachment_path: str | None = None


@dataclass
class _Progress:
    """How far a review got; recorded on the row whichever way it ends."""

    action: str | None = None
    page_id: str | None = None
    executed: bool = False
    follow_up_sent: bool = False


def checkpoint_id_of(snapshot: Any) -> str:
    """The checkpoint id in a `StateSnapshot`, or "" when it has none."""
    configurable = (getattr(snapshot, "config", None) or {}).get("configurable") or {}
    return str(configurable.get("checkpoint_id") or "")


async def current_turn_ref(graph: Any, config: Mapping[str, Any]) -> str:
    """The thread's latest checkpoint id, or "" when it cannot be read."""
    try:
        return checkpoint_id_of(await graph.aget_state(config))
    except Exception:
        return ""


async def _finalize(
    review_id: uuid.UUID,
    *,
    verdict: str,
    reason: str,
    progress: _Progress | None = None,
) -> bool:
    from app.tools import interaction_reviews

    progress = progress or _Progress()
    return await interaction_reviews.finalize(
        review_id,
        verdict=cast(interaction_reviews.ReviewVerdict, verdict),
        reason=reason,
        action=progress.action,
        action_page_id=progress.page_id,
        executed=progress.executed,
        follow_up_sent=progress.follow_up_sent,
    )


async def _skip(
    review_id: uuid.UUID, *, reason: str, intent: str | None, progress: _Progress | None = None
) -> None:
    if await _finalize(review_id, verdict="skipped", reason=reason, progress=progress):
        log.info("interaction_review.skipped", reason=reason, intent=intent)


async def finalize_skipped(
    review_id: uuid.UUID | None, *, reason: str, intent: str | None = None
) -> None:
    """Finalize a job as skipped unless it is already final. Never raises.

    The listener calls this after cancelling a review: a cancelled coroutine
    may not reach its own handler (a cancel during the start delay, before
    `review_turn` runs), so the cancelling side closes the row too. The first
    finalize wins; later ones change nothing and log nothing.
    """
    if review_id is None:
        return
    try:
        await _skip(review_id, reason=reason, intent=intent)
    except Exception as exc:
        log.warning("interaction_review.store_failed", error_type=type(exc).__name__)


async def finalize_interrupted(review_id: uuid.UUID) -> None:
    """Finalize a claimed job a stopped process left `executing`. Never raises.

    The row is closed `error(interrupted)` and keeps the action, page, and
    progress it recorded. It is never re-reviewed: its Notion write may
    already have run (and is idempotent), and a follow-up that may already
    have been delivered is not repeated.
    """
    try:
        await _finalize(review_id, verdict="error", reason="interrupted")
    except Exception as exc:
        log.warning("interaction_review.store_failed", error_type=type(exc).__name__)


def _titles(open_list: Sequence[Mapping[str, str]], completed: Sequence[Mapping[str, str]]) -> dict[str, str]:
    titles = {str(task["id"]): str(task.get("title") or "") for task in completed}
    titles.update({str(task["id"]): str(task.get("title") or "") for task in open_list})
    return titles


def _number(props: Mapping[str, Any], key: str, default: int) -> int:
    prop = props.get(key)
    value = prop.get("number") if isinstance(prop, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return int(value)


async def reward_metadata(page_id: str) -> tuple[str, str, int]:
    """The page's reward sizing: `(work_type, energy_required, time_estimate)`.

    Read from the page's `Work Type`, `Energy Required`, and
    `Time Estimate (min)` properties, the fields `complete_node` takes from
    the active task. Fail-soft: a read error logs its type only and returns
    the defaults `maybe_reward` applies (`""`, `""`, 30), so a lookup never
    costs the celebration.
    """
    from app.graph.nodes._task_match import extract_select
    from app.tools import notion

    try:
        page = await notion.get_page(page_id=page_id)
    except Exception as exc:
        log.warning("interaction_review.page_read_failed", error_type=type(exc).__name__)
        return "", "", _DEFAULT_TIME_ESTIMATE
    raw = page.get("properties") if isinstance(page, Mapping) else None
    props: dict[str, Any] = raw if isinstance(raw, dict) else {}
    return (
        extract_select(props, "Work Type"),
        extract_select(props, "Energy Required"),
        _number(props, "Time Estimate (min)", _DEFAULT_TIME_ESTIMATE),
    )


async def _complete(
    *,
    review_id: uuid.UUID,
    peer: str,
    page_id: str,
    title: str,
    kind: str,
    state: Mapping[str, Any],
    now: datetime,
    progress: _Progress,
) -> _Execution:
    from app.tools import interaction_reviews, notion, reminders
    from app.tools.rewards import maybe_reward

    await notion.update_status(page_id=page_id, new_status="Completed")
    progress.executed = True
    # Recorded as soon as it lands, so a crash after this point leaves a row
    # that says the write ran.
    await interaction_reviews.mark_executed(review_id)
    if kind == "reminder":
        # A reminder finished before it fired must not fire afterwards. The
        # worker's pre-send check covers a row this call fails to cancel.
        try:
            await reminders.cancel_pending_reminders(peer=peer, notion_page_id=page_id)
        except Exception as exc:
            log.warning(
                "interaction_review.reminder_cancel_failed",
                page_id=page_id,
                error_type=type(exc).__name__,
            )
    try:
        # A completed task's queued deadline nudges must not fire either.
        await reminders.cancel_pending_nudges(peer=peer, notion_page_id=page_id)
    except Exception as exc:
        log.warning(
            "interaction_review.nudge_cancel_failed",
            page_id=page_id,
            error_type=type(exc).__name__,
        )
    try:
        await reminders.resolve_recent_outbound(
            peer=peer, signal_timestamp=0, notion_page_id=page_id
        )
    except Exception as exc:
        log.warning(
            "interaction_review.recent_outbound_clear_failed",
            page_id=page_id,
            error_type=type(exc).__name__,
        )

    streak = int(state.get("streak") or 0) + 1
    reward_text = ""
    attachment: str | None = None
    work_type, energy_required, time_estimate = await reward_metadata(page_id)
    try:
        reward = await maybe_reward(
            peer=peer,
            task_title=title,
            notion_page_id=page_id,
            streak=streak,
            work_type=work_type,
            energy_required=energy_required,
            time_estimate=time_estimate,
        )
        reward_text = reward["text"]
        attachment = reward["attachment_path"]
    except Exception as exc:
        # The completion stands without a celebration; a reward failure must
        # not take back a write the user asked for.
        log.warning(
            "interaction_review.reward_failed",
            page_id=page_id,
            error_type=type(exc).__name__,
        )

    updates: dict[str, Any] = {
        "recent_tasks": record_task_event(
            state.get("recent_tasks"),
            page_id=page_id,
            title=title,
            kind="reminder" if kind == "reminder" else "task",
            event="completed",
            now=now,
        ),
        "streak": streak,
        "tasks_completed_today": int(state.get("tasks_completed_today") or 0) + 1,
        "conversation_state": "idle",
    }
    active = state.get("active_task")
    if isinstance(active, Mapping) and active.get("page_id") == page_id:
        updates["active_task"] = None
    return _Execution(page_id, title, updates, reward_text, attachment)


async def _execute(
    verdict: Verdict,
    *,
    review_id: uuid.UUID,
    peer: str,
    state: Mapping[str, Any],
    open_list: Sequence[Mapping[str, str]],
    completed: Sequence[Mapping[str, str]],
    now: datetime,
    progress: _Progress,
) -> _Execution:
    titles = _titles(open_list, completed)
    kinds = {str(task["id"]): str(task.get("kind") or "task") for task in open_list}
    page_id = verdict.page_id or ""

    if verdict.action == "complete_task":
        return await _complete(
            review_id=review_id,
            peer=peer,
            page_id=page_id,
            title=titles.get(page_id, ""),
            kind=kinds.get(page_id, "task"),
            state=state,
            now=now,
            progress=progress,
        )

    # send_only: nothing is written; the follow-up is the whole correction.
    return _Execution(page_id, titles.get(page_id, ""), {})


async def _send_follow_up(
    *, peer: str, turn_ref: str, body: str, attachment_path: str | None
) -> str | None:
    """Send the follow-up; return the delivered body, or None when sending failed."""
    from app.tools import signal_client

    key_source = f"interaction_review:{turn_ref}:{peer}:{body}"
    kwargs: dict[str, Any] = {
        "idempotency_key": hashlib.sha256(key_source.encode()).hexdigest()[:32],
    }
    if attachment_path:
        kwargs["attachment_paths"] = [attachment_path]
    try:
        await signal_client.send_message(recipient=peer, message=body, **kwargs)
    except Exception as exc:
        log.warning(
            "interaction_review.follow_up_send_failed",
            error_type=type(exc).__name__,
            attachment_count=1 if attachment_path else 0,
        )
        return None
    return body


async def _maybe_alert(settings: ReviewSettings) -> None:
    from app.tools import interaction_reviews, ops_alerts

    try:
        count = await interaction_reviews.count_executed_corrections(
            peer=None, window_seconds=_DAY_SECONDS
        )
        if not exceeds_alert_threshold(count, settings.alert_threshold):
            return
        # Counts only: an ops alert body never carries a peer, title, or reason.
        await ops_alerts.enqueue(
            kind="interaction_review_excess",
            body=(
                f"Interaction review executed {count} corrections in 24 h "
                f"(threshold {settings.alert_threshold}); see the interaction_reviews table."
            ),
            severity="warning",
        )
    except Exception as exc:
        log.warning("interaction_review.alert_check_failed", error_type=type(exc).__name__)


async def review_turn(
    *,
    peer: str,
    final_state: Mapping[str, Any],
    graph: Any,
    config: Mapping[str, Any],
    review_id: uuid.UUID,
    turn_ref: str,
    still_current: Callable[[], bool] | None = None,
    claim_execution: Callable[[], bool] | None = None,
) -> None:
    """Review one delivered turn and apply at most one correction.

    `review_id` is the job's pending `interaction_reviews` row; every exit
    path finalizes it. `turn_ref` is the reviewed turn's checkpoint id: the
    follow-up is sent and the checkpoint written only while it is still the
    thread's latest.

    `still_current` returns False once the peer has a newer message waiting;
    the review is then skipped before the model call. `claim_execution` is
    asked right before the first effect: False means the peer spoke in the
    meantime and the correction is dropped; True commits the listener to wait
    for this review (up to its bound) before running the peer's next turn.
    The row is then claimed (`executing`, with the action and page) before
    any external effect, and `executed` / `follow_up_sent` are stored as each
    effect lands.

    Cancellation finalizes the row as skipped, with the cancellation message
    as the reason when it is one of `SKIP_REASONS` (else `cancelled`), and
    records any effect that already ran; then it propagates. Nothing else
    raises: failures are logged and stored.
    """
    from app.tools import interaction_reviews, notion

    settings = review_settings()
    intent = str(final_state.get("intent") or "") or None
    progress = _Progress()
    log.info("interaction_review.start", intent=intent, has_turn_ref=bool(turn_ref))
    try:
        if still_current is not None and not still_current():
            await _skip(review_id, reason="buffer_non_empty", intent=intent)
            return
        executed_last_hour = await interaction_reviews.count_executed_corrections(
            peer=peer, window_seconds=_HOUR_SECONDS
        )
        if is_rate_limited(executed_last_hour, settings.max_per_hour):
            await _skip(review_id, reason="rate_limited", intent=intent)
            return

        from app.graph.nodes._task_match import open_tasks

        now = datetime.now(UTC)
        open_list = open_tasks(await notion.query_all(), include_reminders=True)
        inputs = inputs_from_state(final_state, open_list, now=now)
        text = await judge_turn(inputs)
        verdict = parse_verdict(
            text,
            open_page_ids={task["id"] for task in inputs.open_tasks},
            completed_this_turn={task["id"] for task in inputs.completed_this_turn},
        )
        if verdict is None:
            await _finalize(review_id, verdict="error", reason="invalid_verdict")
            return

        log.info(
            "interaction_review.verdict",
            verdict=verdict.verdict,
            action=verdict.action,
            intent=intent,
            page_id=verdict.page_id,
        )
        progress.action = verdict.action
        if verdict.verdict == "ok":
            await _finalize(review_id, verdict="ok", reason=verdict.reason, progress=progress)
            return

        progress.page_id = verdict.page_id
        if claim_execution is not None and not claim_execution():
            await _skip(review_id, reason="buffer_non_empty", intent=intent, progress=progress)
            return
        # Claimed before the first external effect: from here on a restart
        # finalizes this row instead of reviewing the turn again.
        if not await interaction_reviews.claim(
            review_id, action=verdict.action, page_id=verdict.page_id or ""
        ):
            # Another side already finalized the row; nothing has run.
            log.info("interaction_review.claim_lost", action=verdict.action)
            return

        execution = await _execute(
            verdict,
            review_id=review_id,
            peer=peer,
            state=final_state,
            open_list=open_list,
            completed=inputs.completed_this_turn,
            now=now,
            progress=progress,
        )

        # Guard before sending or writing: when the thread has moved past the
        # reviewed turn, a follow-up would refer to stale context. A Notion
        # write that already ran is recorded on the row.
        if not turn_ref or await current_turn_ref(graph, config) != turn_ref:
            log.warning(
                "interaction_review.stale_checkpoint",
                action=verdict.action,
                has_turn_ref=bool(turn_ref),
            )
            await _finalize(
                review_id, verdict="error", reason="stale_checkpoint", progress=progress
            )
            return

        body = render_task_token(FOLLOW_UP_TEMPLATES[verdict.action], title=execution.title or None)
        if not execution.title:
            # No stored name to put in the token's place: say it without one.
            body = body.replace(TASK_TOKEN, "that one")
        if execution.reward_text:
            body = f"{body} {execution.reward_text}"
        delivered = await _send_follow_up(
            peer=peer,
            turn_ref=turn_ref,
            body=body,
            attachment_path=execution.attachment_path,
        )
        if delivered is not None:
            progress.follow_up_sent = True
            # send_only's entire correction is the delivery.
            if verdict.action == "send_only":
                progress.executed = True
            await interaction_reviews.mark_follow_up_sent(
                review_id, executed=verdict.action == "send_only"
            )
        elif verdict.action == "send_only":
            # Nothing was delivered, so nothing happened: no checkpoint write.
            await _finalize(review_id, verdict="error", reason="send_failed", progress=progress)
            return

        # Written as the terminal node, so the next turn starts fresh at the
        # entry node with the correction in its history, ledger, and state.
        updates: dict[str, Any] = {**execution.updates, "pending_clarification": None}
        if delivered is not None:
            from langchain_core.messages import AIMessage

            updates["messages"] = [AIMessage(content=delivered)]
        await graph.aupdate_state(config, updates, as_node="send")
        await _finalize(review_id, verdict="correct", reason=verdict.reason, progress=progress)
        log.info(
            "interaction_review.corrected",
            action=verdict.action,
            page_id=execution.page_id or None,
            follow_up_sent=delivered is not None,
            named=bool(execution.title),
        )
        await _maybe_alert(settings)
    except asyncio.CancelledError as cancelled:
        message = cancelled.args[0] if cancelled.args else None
        reason = message if message in SKIP_REASONS else "cancelled"
        try:
            await _skip(review_id, reason=str(reason), intent=intent, progress=progress)
        except Exception as store_exc:
            log.warning(
                "interaction_review.store_failed", error_type=type(store_exc).__name__
            )
        raise
    except Exception as exc:
        log.warning("interaction_review.error", error_type=type(exc).__name__, intent=intent)
        try:
            await _finalize(
                review_id, verdict="error", reason=type(exc).__name__, progress=progress
            )
        except Exception as store_exc:
            log.warning(
                "interaction_review.store_failed", error_type=type(store_exc).__name__
            )
