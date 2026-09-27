"""CANNOT_FINISH node: shame-safe breakdown handling.

When the user indicates they can't finish a task, gathers progress info and
creates sub-tasks for the remaining work.

The task is the checkpointed `active_task`. With none, it is the newest task
the recent-task ledger shows as just nudged, reminded, or suggested in the
last 24 h: "I can't finish that today" right after a deadline nudge is about
the nudged task, not a request with no task. Only with neither does the node
answer without a task.

Once the model has the user's progress (`phase: analyze_remaining`), each
remaining sub-task is created in Notion as a hidden child of that task.

Implements docs/ai-prompts/cannot-finish.md behavior.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.graph.context import record_turn_action, render_history, render_recent_tasks
from app.graph.state import OutboundDraft, State

log = structlog.get_logger(__name__)

# Ledger events that make a task the one "I can't finish that" is about: the
# system just put it in front of the user.
_ANCHOR_EVENTS = frozenset({"nudged", "reminded", "suggested"})

# An entry older than this is too stale to be what "that" refers to.
_LEDGER_ANCHOR_FRESHNESS = timedelta(hours=24)

# Sub-tasks written per turn. The breakdown asks for 15-90 minute chunks; a
# longer list is a model that lost the thread, not a plan.
_MAX_SUB_TASKS = 6
_SUB_TASK_TITLE_CHARS = 200
_DEFAULT_SUB_TASK_MINUTES = 30
# docs/ai-prompts/cannot-finish.md asks for 15-90 minute chunks. An estimate
# outside that range is clamped into it rather than dropped: the step is
# still real work the user has left, only its size is off.
_MIN_SUB_TASK_MINUTES = 15
_MAX_SUB_TASK_MINUTES = 90


def _anchor_is_newer(
    active_task: Mapping[str, Any],
    anchor_page_id: str,
    entries: Iterable[object] | None,
) -> bool:
    """True when the fresh ledger anchor's event is more recent than the active task's selection.

    Both timestamps must be present and parseable; any missing or bad timestamp
    falls back to False so `active_task` retains its default priority.
    """
    active_at_str = str(active_task.get("selected_at") or "")
    if not active_at_str:
        return False
    for raw in entries or []:
        if not isinstance(raw, Mapping) or raw.get("page_id") != anchor_page_id:
            continue
        raw_at = raw.get("at")
        if not isinstance(raw_at, str) or not raw_at:
            return False
        try:
            anchor_at = datetime.fromisoformat(raw_at.replace("Z", "+00:00"))
            anchor_at = anchor_at.replace(tzinfo=UTC) if anchor_at.tzinfo is None else anchor_at.astimezone(UTC)
            active_at = datetime.fromisoformat(active_at_str.replace("Z", "+00:00"))
            active_at = active_at.replace(tzinfo=UTC) if active_at.tzinfo is None else active_at.astimezone(UTC)
            return anchor_at > active_at
        except ValueError:
            return False
    return False


def ledger_anchor(
    entries: Iterable[object] | None, *, now: datetime
) -> tuple[str, str] | None:
    """Return `(page_id, title)` of the newest fresh nudged/reminded/suggested entry.

    The ledger is stored newest first with one entry per page, so the first
    entry whose latest event is nudged, reminded, or suggested is the task the
    system most recently put in front of the user. An entry with no title is
    skipped: a reply that cannot name its task is the failure this fallback
    exists to avoid. An entry older than `_LEDGER_ANCHOR_FRESHNESS`, or with
    an unparseable timestamp, is skipped. An entry with no timestamp is kept
    (only a static eval fixture omits one).
    """
    for raw in entries or []:
        if not isinstance(raw, Mapping):
            continue
        if raw.get("event") not in _ANCHOR_EVENTS:
            continue
        page_id = raw.get("page_id")
        title = raw.get("title")
        if not isinstance(page_id, str) or not page_id:
            continue
        if not isinstance(title, str) or not title.strip():
            continue
        raw_at = raw.get("at")
        if raw_at not in (None, ""):
            if not isinstance(raw_at, str):
                continue
            try:
                at = datetime.fromisoformat(raw_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            at = at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)
            if now - at > _LEDGER_ANCHOR_FRESHNESS:
                continue
        return page_id, title.strip()
    return None


def remaining_sub_tasks(parsed: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Return the sub-tasks to create from a parsed model response.

    Only `phase: analyze_remaining` yields sub-tasks. Each needs a non-empty
    title (cut to `_SUB_TASK_TITLE_CHARS`). A missing or non-integer estimate
    becomes `_DEFAULT_SUB_TASK_MINUTES`; an integer estimate is clamped into
    15-90 minutes. A missing sequence becomes its list position.
    At most `_MAX_SUB_TASKS` are returned, in the model's order.
    """
    if not isinstance(parsed, Mapping) or parsed.get("phase") != "analyze_remaining":
        return []
    raw_list = parsed.get("remaining_sub_tasks")
    if not isinstance(raw_list, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for raw in raw_list:
        if len(cleaned) >= _MAX_SUB_TASKS:
            break
        if not isinstance(raw, Mapping):
            continue
        title = raw.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        minutes = raw.get("time_estimate_minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int):
            minutes = _DEFAULT_SUB_TASK_MINUTES
        minutes = min(max(minutes, _MIN_SUB_TASK_MINUTES), _MAX_SUB_TASK_MINUTES)
        sequence = raw.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            sequence = len(cleaned) + 1
        cleaned.append(
            {
                "title": " ".join(title.split())[:_SUB_TASK_TITLE_CHARS],
                "time_estimate_minutes": minutes,
                "sequence": sequence,
            }
        )
    return cleaned


def _select(props: Mapping[str, Any], key: str) -> str:
    prop = props.get(key)
    select = prop.get("select") if isinstance(prop, Mapping) else None
    name = select.get("name") if isinstance(select, Mapping) else None
    return name if isinstance(name, str) else ""


def _number(props: Mapping[str, Any], key: str) -> int | None:
    prop = props.get(key)
    value = prop.get("number") if isinstance(prop, Mapping) else None
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


async def _ledger_task_details(page_id: str) -> dict[str, Any]:
    """Read the anchored page's estimate, work type, and energy. Fail-soft: {}."""
    try:
        from app.tools import notion

        page = await notion.get_page(page_id=page_id)
        props = page.get("properties", {}) if isinstance(page, dict) else {}
        if not isinstance(props, Mapping):
            return {}
        details: dict[str, Any] = {}
        estimate = _number(props, "Time Estimate (min)")
        if estimate:
            details["time_estimate"] = estimate
        if work_type := _select(props, "Work Type"):
            details["work_type"] = work_type
        if energy := _select(props, "Energy Required"):
            details["energy_required"] = energy
        if status := _select(props, "Status"):
            details["status"] = status
        return details
    except Exception as exc:
        log.info(
            "cannot_finish_node.ledger_task_lookup_failed",
            error_type=type(exc).__name__,
        )
        return {}


async def cannot_finish_node(state: State) -> dict[str, Any]:
    """CANNOT_FINISH handler: gather progress and break down remaining work."""
    peer = state.get("peer", "")

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.models import llm
        from app.prompts.loader import render_with_defaults

        incoming = state.get("incoming", "")
        active_task = state.get("active_task")
        now = datetime.now(UTC)

        turn_actions = list(state.get("turn_actions") or [])

        anchor = ledger_anchor(state.get("recent_tasks"), now=now)

        task: dict[str, Any] = {}
        source = "none"
        use_anchor = anchor is not None and (
            not active_task
            or _anchor_is_newer(active_task, anchor[0], state.get("recent_tasks"))
        )
        if use_anchor and anchor is not None:
            page_id, title = anchor
            task = {"page_id": page_id, "title": title}
            task.update(await _ledger_task_details(page_id))
            source = "ledger"
            # Reminder delivery marks the page Completed; reopen it so sub-tasks
            # are children of an active (non-terminal) page.
            if task.get("status") == "Completed":
                try:
                    from app.tools import notion

                    await notion.update_property(
                        page_id=page_id,
                        prop_json={"properties": {"Status": {"select": {"name": "Pending"}}}},
                    )
                    turn_actions = record_turn_action(
                        turn_actions, action="notion.update_status", page_id=page_id
                    )
                    log.info("cannot_finish_node.reopened", page_id=page_id)
                except Exception:
                    log.exception("cannot_finish_node.reopen_failed", page_id=page_id)
        elif active_task:
            task = dict(active_task)
            source = "active_task"

        real_title = str(task.get("title") or "").strip()
        task_title = real_title or "your task"
        page_id = str(task.get("page_id") or "")
        time_estimate = task.get("time_estimate") or 30

        prompt_text = render_with_defaults(
            "cannot_finish.md.j2",
            {
                "task_title": task_title,
                "time_estimate": time_estimate,
                "user_message": incoming,
                "conversation_history": render_history(state.get("messages")),
                "recent_tasks": render_recent_tasks(state.get("recent_tasks"), now=now),
            },
            defaults={
                "task_title": "your task",
                "time_estimate": 30,
                "user_message": "",
                "conversation_history": "No prior context.",
                "recent_tasks": "None yet.",
            },
        )

        model = llm("medium", caller="cannot_finish")
        messages = [
            SystemMessage(content=prompt_text),
            HumanMessage(content=incoming),
        ]

        response = await model.ainvoke(messages)
        response_text = str(response.content).strip()

        parsed = _parse_json(response_text)
        user_message = _parse_cannot_finish_response(response_text)

        sub_tasks = remaining_sub_tasks(parsed) if page_id else []
        created = 0
        if sub_tasks:
            from app.tools import notion

            for sub in sub_tasks:
                try:
                    sub_page = await notion.create_task(
                        title=sub["title"],
                        work_type=str(task.get("work_type") or "focus"),
                        urgency=int(task.get("urgency") or 50),
                        time_estimate=sub["time_estimate_minutes"],
                        energy_required=str(task.get("energy_required") or "Medium"),
                        parent_id=page_id,
                        sequence=sub["sequence"],
                    )
                except Exception:
                    log.exception("cannot_finish_node.subtask_create_failed", page_id=page_id)
                    continue
                sub_id = (sub_page or {}).get("id")
                if sub_id:
                    created += 1
                    turn_actions = record_turn_action(
                        turn_actions, action="notion.create_task", page_id=str(sub_id)
                    )

        draft: OutboundDraft = {
            "recipient": peer,
            "body": user_message,
            "notion_page_id": page_id or None,
        }
        if real_title:
            draft["notion_page_title"] = real_title

        log.info(
            "cannot_finish_node.response",
            has_peer=bool(peer),
            page_id=page_id,
            task_source=source,
            phase=str(parsed.get("phase") or "") if parsed else "",
            sub_task_count=len(sub_tasks),
            created_count=created,
        )
        return {
            "pending_outbound": [draft],
            "conversation_state": "active",
            "turn_actions": turn_actions,
        }

    except Exception:
        log.exception("cannot_finish_node.error", has_peer=bool(peer))
        fallback: OutboundDraft = {
            "recipient": peer,
            "body": "No worries — that task was bigger than it looked. What did you get into before stopping?",
            "notion_page_id": None,
        }
        return {"pending_outbound": [fallback]}


def _parse_json(response_text: str) -> dict[str, Any] | None:
    json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
    if not json_match:
        return None
    try:
        data = json.loads(json_match.group())
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _parse_cannot_finish_response(response_text: str) -> str:
    """Extract the user-facing message from the LLM JSON response.

    Only `progress_question` (phase=ask_progress) and `next_sub_task_message`
    (phase=analyze_remaining) are user-facing. Models fill any `user_message`
    field with an echo of the user's own words, so it must never be selected —
    replying to "this is too big" with "this is too big" is the failure mode
    this ordering guards against.
    """
    data = _parse_json(response_text)
    if data is not None:
        msg = data.get("progress_question") or data.get("next_sub_task_message")
        if msg:
            return str(msg)
    # Valid JSON with no user-facing field, or no JSON at all: never send raw
    # JSON (or an echoed user_message) to the user — ask the progress question.
    return "No worries — what did you get into before stopping?"
