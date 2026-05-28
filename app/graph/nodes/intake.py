"""ADD_TASK node: task intake with label inference, sub-task generation, reminder detection.

Ports docs/ai-prompts/intake.md (464 lines) behavior:
- Aggressive label inference (urgency, work_type, time_estimate)
- Sub-task generation for every task
- Reminder detection (wall-clock time → outbox row)
- Clarification flow (max 3 questions)
- Reschedule from recent_outbound context

When a reminder is detected:
- Creates Notion row via app/tools/notion.create_reminder()
- Enqueues outbox row via app/tools/reminders.enqueue()
- Uses app/tools/time_context to resolve user-local time

REMINDER PERSISTENCE uses the outbox state machine; the reminder worker (APScheduler) handles delivery.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

import structlog

from app.graph.state import OutboundDraft, State

log = structlog.get_logger(__name__)

async def intake_node(state: State) -> dict[str, Any]:
    """ADD_TASK handler: infer labels, generate sub-tasks, detect reminders."""
    peer = state.get("peer", "")

    try:
        from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage

        from app.models import llm
        from app.tools import notion
        from app.tools.time_context import get_time_context

        incoming = state.get("incoming", "")
        user_prefs = state.get("user_prefs", {})
        messages_history: list[AnyMessage] = state.get("messages", [])

        # Build conversation history summary
        history_lines = []
        for msg in messages_history[-6:]:
            role = getattr(msg, "type", "message")
            content = str(getattr(msg, "content", ""))
            history_lines.append(f"{role}: {content[:200]}")
        conversation_history = "\n".join(history_lines) or "No prior context."

        # Get time context in user's timezone
        time_ctx = get_time_context("now")
        current_time = time_ctx["reference_local"]
        user_timezone = time_ctx["user_timezone"]

        # User preferences context
        user_prefs_context = _build_prefs_context(user_prefs)

        # Clarification count tracking (use messages history length as proxy)
        clarification_count = 0

        from app.prompts.loader import render_with_defaults
        prompt_context = {
            "user_message": incoming,
            "conversation_history": conversation_history,
            "user_preferences_context": user_prefs_context,
            "clarification_count": clarification_count,
            "current_time": current_time,
            "user_timezone": user_timezone,
        }
        prompt_text = render_with_defaults("intake.md.j2", prompt_context)

        model = llm("medium", caller="intake")
        messages = [
            SystemMessage(content=prompt_text),
            HumanMessage(content=f"The user said: {incoming!r}"),
        ]

        response = await model.ainvoke(messages)
        response_text = str(response.content).strip()

        parsed = _parse_intake_response(response_text)

        if parsed.get("action") == "clarify":
            question = parsed.get("clarification_question", "Which task are you thinking of?")
            draft = {
                "recipient": peer,
                "body": question,
                "notion_page_id": None,
            }
            return {"pending_outbound": [draft]}

        # Action is "save"
        task_title = parsed.get("title", incoming[:200])
        work_type = parsed.get("work_type", "focus")
        urgency = int(parsed.get("urgency", 50))
        time_estimate = int(parsed.get("time_estimate_minutes", 30))
        energy_required = parsed.get("energy_required", "Medium")
        is_reminder = bool(parsed.get("is_reminder", False))
        remind_at_str = parsed.get("remind_at")
        due_at_str = parsed.get("due_at")
        inline_steps = parsed.get("inline_steps", "")
        use_hidden_subtasks = bool(parsed.get("use_hidden_subtasks", False)) and not is_reminder
        sub_tasks = parsed.get("sub_tasks", [])
        confirmation_message = parsed.get("confirmation_message", f"Got it — {work_type}, ~{time_estimate} min.")

        # Privacy: log only flags / numeric labels, never raw LLM-extracted
        # strings. has_remind_at / has_due_at are booleans; urgency /
        # time_estimate are numeric. Per DEV-AGENTS.md (Safety -> Don't
        # leak private examples), raw `remind_at` / `due_at` may contain
        # echoes of the user's task text.
        log.info(
            "intake_node.parsed",
            is_reminder=is_reminder,
            has_remind_at=bool(remind_at_str),
            has_due_at=bool(due_at_str),
            urgency=urgency,
            time_estimate=time_estimate,
        )

        # Parse due_at (deadline) — privacy-safe failure: no value, no exception.
        deadline_at: datetime | None = None
        if due_at_str and not is_reminder:
            try:
                deadline_at = _parse_iso_strict(due_at_str)
            except ValueError:
                # Privacy: do not log the raw string or exception. The LLM
                # produced an unparseable due_at; treat as if no deadline
                # was given.
                log.info(
                    "intake_node.due_at_parse_failed",
                    has_due_at=True,
                    parse_failed=True,
                )
                deadline_at = None

        if is_reminder and remind_at_str:
            notion_page = await _create_reminder(
                peer=peer,
                title=task_title,
                work_type=work_type,
                energy_required=energy_required,
                remind_at_str=remind_at_str,
            )
        else:
            log.info("intake_node.no_reminder", has_remind_at=bool(remind_at_str), is_reminder=is_reminder)
            notion_page = await notion.create_task(
                title=task_title,
                work_type=work_type,
                urgency=urgency,
                time_estimate=time_estimate,
                energy_required=energy_required,
                inline_steps=inline_steps,
                due_at_iso=deadline_at.isoformat() if deadline_at else None,
            )

        page_id = (notion_page or {}).get("id")

        # Inline deadline-series scheduling. Runs after task creation when a
        # deadline was extracted. Failures are silent from the user's
        # perspective — the daemon backstop (jobs.reminder_scheduler) will
        # retry on the next nightly cycle. We never surface infra retry
        # state to the user (psy-001 / docs/ai-prompts/shared.md).
        assigned_slots: list[tuple[str, datetime]] = []
        if deadline_at is not None and page_id:
            assigned_slots = await _schedule_deadline_series(
                page_id=page_id,
                title=task_title,
                peer=peer,
                deadline_at=deadline_at,
                urgency=urgency,
                user_timezone=user_timezone,
            )

        # Create hidden sub-tasks if needed
        if use_hidden_subtasks and sub_tasks and page_id:
            for sub in sub_tasks:
                try:
                    await notion.create_task(
                        title=sub.get("title", "Sub-task"),
                        work_type=work_type,
                        urgency=urgency,
                        time_estimate=sub.get("time_estimate_minutes", 15),
                        energy_required=energy_required,
                        parent_id=page_id,
                        sequence=sub.get("sequence", 1),
                    )
                except Exception:
                    log.exception("intake_node.subtask_create_failed", page_id=page_id)

        # Append deterministic reminder summary to the confirmation when the
        # deadline series was scheduled. The summary is computed from
        # assigned_slots (not the LLM) to avoid hallucinated times. On
        # scheduling failure we do NOT append anything — the daemon backstop
        # will pick this task up later, and surfacing infra/retry state to
        # the user violates the shame-safe / no-tool-narration contract in
        # docs/ai-prompts/shared.md.
        if assigned_slots:
            try:
                from app.scheduler.deadline_planner import format_reminder_summary
                summary = format_reminder_summary(assigned_slots, user_timezone)
                if summary:
                    confirmation_message = f"{confirmation_message} {summary}"
            except Exception:
                log.exception("intake_node.summary_format_failed")

        draft = {
            "recipient": peer,
            "body": confirmation_message,
            "notion_page_id": page_id,
        }

        log.info(
            "intake_node.saved",
            peer=peer,
            page_id=page_id,
            is_reminder=is_reminder,
            has_due_at=deadline_at is not None,
            scheduled_count=len(assigned_slots),
        )
        return {
            "pending_outbound": [draft],
            "conversation_state": "idle",
        }

    except Exception:
        log.exception("intake_node.error", peer=peer)
        fallback: OutboundDraft = {
            "recipient": peer,
            "body": "Got it, I'll add that to your list.",
            "notion_page_id": None,
        }
        return {"pending_outbound": [fallback]}


async def _create_reminder(
    *,
    peer: str,
    title: str,
    work_type: str,
    energy_required: str,
    remind_at_str: str,
) -> dict[str, Any]:
    """Create a Notion reminder row and enqueue the outbox row.

    notion.create_reminder() accepts: title, remind_at_iso, work_type, energy_required.
    Urgency, time_estimate, and inline_steps are set by the Notion function internally.
    """
    from app.tools import notion
    from app.tools.db import get_db_conn
    from app.tools.reminders import enqueue

    # Parse remind_at to datetime
    remind_at = _parse_remind_at(remind_at_str)

    notion_page = await notion.create_reminder(
        title=title,
        remind_at_iso=remind_at.isoformat(),
        work_type=work_type,
        energy_required=energy_required,
    )

    page_id = (notion_page or {}).get("id", "")

    # Enqueue in the outbox for at-least-once delivery
    if page_id:
        idempotency_key = f"intake-{page_id}"
        try:
            async with get_db_conn() as conn:
                await enqueue(
                    conn,
                    notion_page_id=page_id,
                    peer=peer,
                    body=f"Hey — {title}",
                    due_at=remind_at,
                    idempotency_key=idempotency_key,
                )
            log.info(
                "intake_node.outbox_enqueued",
                page_id=page_id,
                due_at=remind_at.isoformat() if remind_at else None,
                idempotency_key=idempotency_key,
            )
        except Exception:
            log.exception("intake_node.enqueue_failed", page_id=page_id)
            # Notion row exists but outbox write failed — reminder won't be delivered
            # automatically. Emit ops alert so the operator can investigate.
            try:
                from app.tools import ops_alerts
                await ops_alerts.enqueue(
                    kind="reminder_enqueue_failed",
                    body=f"Reminder outbox enqueue failed for page {page_id!r}. Reminder exists in Notion but will not be delivered until the outbox row is created.",
                    severity="warning",
                )
            except Exception:
                log.exception("intake_node.ops_alert_failed", page_id=page_id)

    return notion_page or {}


def _parse_remind_at(remind_at_str: str) -> datetime:
    """Parse an ISO 8601 string to a UTC-aware datetime.

    Caller is responsible for privacy-safe error handling: this function may
    raise ValueError, but its exception message is intentionally generic
    (no reflected input) so structlog .exception() output does not echo
    user content.
    """
    try:
        normalized = remind_at_str.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (ValueError, TypeError) as exc:
        # Privacy: do NOT include the raw value in the message. The caller's
        # log emission will surface this exception via log.exception(); the
        # message text must not echo LLM-controlled input.
        raise ValueError("invalid ISO timestamp") from exc


def _parse_iso_strict(iso_str: str) -> datetime:
    """Parse an ISO 8601 string to a UTC datetime; privacy-safe error.

    Raises ValueError with a generic message (no reflected input) so callers
    can log a flag-only failure without leaking the offending value.
    """
    if not isinstance(iso_str, str) or not iso_str.strip():
        raise ValueError("empty ISO timestamp")
    try:
        normalized = iso_str.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except (ValueError, TypeError) as exc:
        raise ValueError("malformed ISO timestamp") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


async def _schedule_deadline_series(
    *,
    page_id: str,
    title: str,
    peer: str,
    deadline_at: datetime,
    urgency: int,
    user_timezone: str,
) -> list[tuple[str, datetime]]:
    """Inline-schedule the milestone series after task creation.

    Returns the list of (label, assigned_slot) pairs that were successfully
    enqueued, or an empty list on any failure path. Failures are silent
    from the user's perspective — the nightly reminder_scheduler daemon
    re-tries via the orphan query (tasks with Due At but no Reminder
    Scheduled At). This is the design contract: never leak infra retry
    state into user-facing confirmations (psy-001).
    """
    try:
        from app.scheduler.reminder_scheduling import schedule_for_task
        from app.tools import notion

        now = datetime.now(UTC)
        assigned, failures = await schedule_for_task(
            page_id,
            title,
            peer,
            deadline_at,
            urgency,
            now=now,
            user_tz=user_timezone,
        )

        if not failures and assigned:
            # Mark Reminder Scheduled At so the daemon orphan query excludes
            # this task on its next run.
            try:
                await notion.mark_reminder_scheduled(page_id)
            except Exception:
                log.exception("intake_node.mark_scheduled_failed", page_id=page_id)

        # Privacy: log only counts, not slot datetimes.
        log.info(
            "intake_node.deadline_series_scheduled",
            page_id=page_id,
            scheduled_count=len(assigned),
            failure_count=len(failures),
        )
        return assigned
    except Exception:
        # Privacy: do not include the exception string. The daemon will
        # retry via the unscheduled-deadline filter on its next run.
        log.exception("intake_node.deadline_series_failed", page_id=page_id)
        return []


def _parse_intake_response(response_text: str) -> dict[str, Any]:
    """Extract JSON from LLM intake response."""
    # Try to find a JSON block
    json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
    if json_match:
        try:
            loaded = json.loads(json_match.group())
            if isinstance(loaded, dict):
                return cast(dict[str, Any], loaded)
        except json.JSONDecodeError:
            pass

    # Fallback: return a save action with the raw text as title
    return {
        "action": "save",
        "title": response_text[:200],
        "work_type": "focus",
        "urgency": 50,
        "time_estimate_minutes": 30,
        "energy_required": "Medium",
        "is_reminder": False,
        "remind_at": None,
        "due_at": None,
        "use_hidden_subtasks": False,
        "sub_tasks": [],
        "inline_steps": "",
        "confirmation_message": "Got it — added.",
    }


def _build_prefs_context(user_prefs: Mapping[str, object]) -> str:
    """Build user preferences context string for the prompt."""
    if not user_prefs:
        return "No user preferences configured."

    lines = ["This user has the following preferences:"]
    if tz := user_prefs.get("timezone"):
        lines.append(f"- Timezone: {tz}")
    wt = user_prefs.get("preferred_work_types")
    if isinstance(wt, list) and all(isinstance(item, str) for item in wt):
        lines.append(f"- Preferred work types: {', '.join(wt)}")
    if energy := user_prefs.get("default_energy"):
        lines.append(f"- Default energy level: {energy}")
    return "\n".join(lines)
