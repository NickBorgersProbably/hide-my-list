"""Reminder scheduler daemon — nightly backstop for deadline-driven reminders.

Runs once daily at 04:00 user-local. Two responsibilities:

1. Orphan catch-up.
   Query Notion for tasks with Due At set but no Reminder Scheduled At
   (intake's inline scheduling failed). Schedule the milestone series via
   reminder_scheduling.schedule_for_task.

2. Deadline-edit detection.
   Query Notion for tasks that already have a Reminder Scheduled At AND a
   Due At. Cross-check current Notion Due At against the ledger's
   deadline_at (+-60s tolerance). On mismatch: supersede the active ledger
   rows, mark the corresponding outbox rows dead, and schedule a fresh
   milestone series against the new deadline.

The dual-query is required because the orphan-catch-up filter
(Reminder Scheduled At is_empty) excludes already-scheduled tasks. Without a
second query that explicitly fetches the scheduled set, deadline edits would
never be observed.

Failure modes:
  Notion 5xx     -> ops alert, retry next day.
  Per-task fail  -> ops alert, continue with next task.
  Slot fallback  -> warning logged by deadline_planner; ops alert raised by
                    reminder_scheduling.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Tolerance for matching ledger.deadline_at against Notion Due At. Notion
# stores ISO-8601 with second precision; round-tripping through Postgres can
# introduce sub-second drift even when the underlying value is unchanged.
# Anything within +-_DEADLINE_TOLERANCE_SECONDS is treated as "unchanged".
_DEADLINE_TOLERANCE_SECONDS = 60


async def run_reminder_scheduler() -> None:
    """Entry point for the APScheduler reminder_scheduler job.

    Wraps _run_once() so APScheduler can invoke a no-arg callable; tests can
    drive _run_once() directly with injected dependencies if needed.
    """
    await _run_once()


async def _run_once() -> None:
    from app.tools import notion, ops_alerts

    try:
        orphans = await notion.query_tasks_with_unscheduled_deadlines()
    except Exception:
        # Privacy: do not log exception strings (may echo headers / IDs).
        log.exception("reminder_scheduler.orphans_query_failed")
        try:
            await ops_alerts.enqueue(
                kind="reminder_scheduler_notion_failed",
                body="reminder_scheduler: orphan-query Notion call failed; will retry next cycle.",
                severity="warning",
            )
        except Exception:
            log.exception("reminder_scheduler.ops_alert_failed")
        orphans = {"results": []}

    try:
        scheduled = await notion.query_scheduled_tasks_with_deadlines()
    except Exception:
        log.exception("reminder_scheduler.scheduled_query_failed")
        try:
            await ops_alerts.enqueue(
                kind="reminder_scheduler_notion_failed",
                body="reminder_scheduler: scheduled-deadline query Notion call failed; will retry next cycle.",
                severity="warning",
            )
        except Exception:
            log.exception("reminder_scheduler.ops_alert_failed")
        scheduled = {"results": []}

    orphan_count = 0
    edit_count = 0

    for page in orphans.get("results", []):
        try:
            handled = await _schedule_orphan(page)
            if handled:
                orphan_count += 1
        except Exception:
            log.exception(
                "reminder_scheduler.orphan_failed",
                page_id=page.get("id", ""),
            )

    for page in scheduled.get("results", []):
        try:
            handled = await _detect_and_reschedule_edit(page)
            if handled:
                edit_count += 1
        except Exception:
            log.exception(
                "reminder_scheduler.edit_detection_failed",
                page_id=page.get("id", ""),
            )

    log.info(
        "reminder_scheduler.cycle_done",
        orphans_handled=orphan_count,
        edits_handled=edit_count,
    )


async def _schedule_orphan(page: dict[str, Any]) -> bool:
    """Schedule a milestone series for an unscheduled task."""
    from app.scheduler.reminder_scheduling import schedule_for_task
    from app.tools import notion, ops_alerts

    page_id = page.get("id", "")
    if not page_id:
        return False

    parsed = _parse_page_for_scheduling(page)
    if parsed is None:
        return False

    title, peer, deadline_at, urgency, user_tz = parsed
    now = datetime.now(UTC)

    assigned_slots, failures = await schedule_for_task(
        page_id,
        title,
        peer,
        deadline_at,
        urgency,
        now=now,
        user_tz=user_tz,
    )

    if not failures and assigned_slots:
        try:
            await notion.mark_reminder_scheduled(page_id)
        except Exception:
            log.exception(
                "reminder_scheduler.mark_scheduled_failed",
                page_id=page_id,
            )

    if failures:
        try:
            await ops_alerts.enqueue(
                kind="reminder_scheduler_partial_failure",
                body=(
                    f"reminder_scheduler: failed to enqueue {len(failures)} "
                    "milestone(s) for orphan task; will retry next cycle."
                ),
                severity="warning",
            )
        except Exception:
            log.exception("reminder_scheduler.ops_alert_failed")

    log.info(
        "reminder_scheduler.orphan_handled",
        page_id=page_id,
        scheduled_count=len(assigned_slots),
        failure_count=len(failures),
    )
    return True


async def _detect_and_reschedule_edit(page: dict[str, Any]) -> bool:
    """If Notion Due At has drifted from the active ledger deadline, reschedule.

    Returns True if an edit was detected and handled (or a rescheduling
    attempt was made), False if no edit was detected.
    """
    from app.scheduler.reminder_scheduling import (
        cancel_outbox_rows,
        get_active_deadline_for_page,
        schedule_for_task,
        supersede_ledger_rows,
    )
    from app.tools import notion, ops_alerts

    page_id = page.get("id", "")
    if not page_id:
        return False

    parsed = _parse_page_for_scheduling(page)
    if parsed is None:
        return False

    title, peer, current_deadline, urgency, user_tz = parsed

    ledger_deadline = await get_active_deadline_for_page(page_id)
    if ledger_deadline is None:
        # No active ledger row even though Reminder Scheduled At is set.
        # That's an inconsistency, but not something the daemon should
        # paper over — log and skip.
        log.info(
            "reminder_scheduler.edit_skip_no_ledger",
            page_id=page_id,
        )
        return False

    drift = abs((current_deadline - ledger_deadline).total_seconds())
    if drift <= _DEADLINE_TOLERANCE_SECONDS:
        return False

    log.info(
        "reminder_scheduler.deadline_edit_detected",
        page_id=page_id,
        drift_seconds=int(drift),
    )

    # Supersede the active ledger rows and dead-letter the outbox entries.
    superseded_outbox_ids = await supersede_ledger_rows(page_id)
    await cancel_outbox_rows(superseded_outbox_ids)

    # Schedule a fresh milestone series against the new deadline.
    now = datetime.now(UTC)
    assigned_slots, failures = await schedule_for_task(
        page_id,
        title,
        peer,
        current_deadline,
        urgency,
        now=now,
        user_tz=user_tz,
    )

    if not failures and assigned_slots:
        try:
            # Refresh Reminder Scheduled At to reflect the new series.
            await notion.mark_reminder_scheduled(page_id)
        except Exception:
            log.exception(
                "reminder_scheduler.mark_scheduled_failed",
                page_id=page_id,
            )

    if failures:
        try:
            await ops_alerts.enqueue(
                kind="reminder_scheduler_partial_failure",
                body=(
                    f"reminder_scheduler: failed to enqueue {len(failures)} "
                    "milestone(s) after deadline edit; will retry next cycle."
                ),
                severity="warning",
            )
        except Exception:
            log.exception("reminder_scheduler.ops_alert_failed")

    log.info(
        "reminder_scheduler.edit_handled",
        page_id=page_id,
        superseded_count=len(superseded_outbox_ids),
        scheduled_count=len(assigned_slots),
        failure_count=len(failures),
    )
    return True


def _parse_page_for_scheduling(
    page: dict[str, Any],
) -> tuple[str, str, datetime, int, str] | None:
    """Extract (title, peer, deadline_at, urgency, user_tz) from a Notion page payload.

    Returns None when required fields are missing or malformed. Logs only
    flags / page_id — never the raw values (privacy contract).
    """
    import os

    page_id = page.get("id", "")
    props = page.get("properties", {}) or {}

    # Due At (date)
    due_at_prop = (props.get("Due At") or {}).get("date") or {}
    due_at_iso = due_at_prop.get("start")
    if not due_at_iso:
        log.info("reminder_scheduler.parse_skip_no_due_at", page_id=page_id)
        return None

    try:
        deadline_at = _parse_iso(due_at_iso)
    except ValueError:
        log.info(
            "reminder_scheduler.parse_skip_bad_due_at",
            page_id=page_id,
            parse_failed=True,
        )
        return None

    # Title — Notion title property is a list of rich_text fragments.
    title_prop = props.get("Title") or props.get("Name") or {}
    title_fragments = title_prop.get("title") or []
    title = "".join(frag.get("plain_text", "") for frag in title_fragments) or "task"

    # Urgency — optional number property; default to 50 (standard tier).
    urgency_prop = props.get("Urgency") or {}
    urgency_val = urgency_prop.get("number")
    urgency = int(urgency_val) if isinstance(urgency_val, (int, float)) else 50

    # Peer — daemon resolves from env. Per-user routing belongs to a
    # follow-up; the single-user deployment uses one peer.
    peer = os.environ.get("OPS_ALERT_SIGNAL_NUMBER", "") or os.environ.get(
        "DEFAULT_PEER", ""
    )
    if not peer:
        log.info("reminder_scheduler.parse_skip_no_peer", page_id=page_id)
        return None

    user_tz = os.environ.get("USER_TZ", "America/Chicago")

    return title, peer, deadline_at, urgency, user_tz


def _parse_iso(iso_str: str) -> datetime:
    """Parse an ISO 8601 string to a tz-aware UTC datetime.

    Raises ValueError on malformed input. Caller is responsible for
    privacy-safe logging (never echo iso_str into log fields).
    """
    normalized = iso_str.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


