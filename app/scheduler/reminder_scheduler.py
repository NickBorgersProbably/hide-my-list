"""Deadline-reminder backstop daemon.

run_reminder_scheduler() is registered as an APScheduler job (daily 04:00 USER_TZ).
It is the backstop for inline scheduling: intake schedules reminders immediately
after task creation, but if that enqueue failed (transient DB error, mid-flight crash)
or if the deadline was later edited in Notion, this daemon catches the gap.

Per cycle:
  1. notion.query_tasks_with_unscheduled_deadlines() — tasks where "Due At" is set
     but "Reminder Scheduled At" is empty and status != Completed.
  2. For each task:
     a. Check reminder_scheduling_ledger for existing non-superseded rows with the
        same notion_page_id.
     b. If a prior deadline exists and differs from the current Notion deadline,
        supersede old ledger rows and cancel the corresponding outbox rows, then
        schedule fresh ones.
     c. If no prior rows exist, schedule from scratch.
     d. Call notion.mark_reminder_scheduled on success.

Failure modes:
  - Notion 5xx: ops_alerts.enqueue + skip entire cycle; retry next day.
  - Per-task planner fallback (load-balancer found no ideal slot): log + ops_alert
    warning; continue with fallback slot.
  - Per-task enqueue failure: ops_alert + skip that task; next nightly run retries
    (Reminder Scheduled At stays empty).
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_USER_TZ = os.environ.get("USER_TZ", "America/Chicago")


async def run_reminder_scheduler() -> None:
    """Backstop daemon: schedule deadline-driven reminders for tasks intake missed.

    Registered as APScheduler job 'reminder_scheduler' (daily 04:00 USER_TZ).
    Called by APScheduler; not called directly from app Python code.
    """
    from app.scheduler.reminder_scheduling import (
        cancel_outbox_rows,
        get_active_deadline_for_page,
        schedule_for_task,
        supersede_ledger_rows,
    )
    from app.tools import notion, ops_alerts

    now = datetime.now(UTC)
    log.info("reminder_scheduler.cycle_start", now=now.isoformat())

    # Step 1: Fetch tasks with unscheduled deadlines from Notion.
    try:
        result = await notion.query_tasks_with_unscheduled_deadlines()
    except Exception as exc:
        log.error("reminder_scheduler.notion_query_failed", error=str(exc))
        try:
            await ops_alerts.enqueue(
                kind="reminder_scheduler_notion_failed",
                body="reminder_scheduler: Notion query failed. Deadline reminders will retry on next nightly run.",
                severity="warning",
            )
        except Exception as inner_exc:
            log.error(
                "reminder_scheduler.ops_alert_failed",
                error=str(inner_exc),
            )
        return

    tasks = result.get("results", [])
    log.info("reminder_scheduler.tasks_found", count=len(tasks))

    if not tasks:
        log.info("reminder_scheduler.no_tasks")
        return

    scheduled_count = 0
    failed_count = 0

    for task in tasks:
        page_id = task.get("id", "")
        if not page_id:
            continue

        props = task.get("properties", {})
        title = _extract_title(props)
        peer = _extract_peer(task)
        deadline_at = _extract_deadline(props)
        urgency = _extract_urgency(props)

        if not deadline_at:
            log.warning(
                "reminder_scheduler.task_missing_deadline",
                page_id=page_id,
            )
            continue

        if not peer:
            log.warning(
                "reminder_scheduler.task_missing_peer",
                page_id=page_id,
            )
            # Use a configured default peer if available; skip otherwise.
            peer = os.environ.get("AUTHORIZED_PEERS", "").split(",")[0].strip()
            if not peer:
                continue

        log.info(
            "reminder_scheduler.processing_task",
            page_id=page_id,
            deadline_at=deadline_at.isoformat(),
            urgency=urgency,
        )

        try:
            # Step 2a: Check if we have prior ledger rows (deadline might have changed).
            prior_deadline = await get_active_deadline_for_page(page_id)

            if prior_deadline is not None:
                # Normalize for comparison (both UTC-aware).
                prior_utc = prior_deadline.astimezone(UTC)
                current_utc = deadline_at.astimezone(UTC)

                if abs((prior_utc - current_utc).total_seconds()) > 60:
                    # Step 2b: Deadline changed — supersede old rows and cancel outbox.
                    log.info(
                        "reminder_scheduler.deadline_changed",
                        page_id=page_id,
                        prior_deadline=prior_utc.isoformat(),
                        new_deadline=current_utc.isoformat(),
                    )
                    superseded_outbox_ids = await supersede_ledger_rows(page_id)
                    await cancel_outbox_rows(superseded_outbox_ids)
                else:
                    # Deadline unchanged but Reminder Scheduled At is empty —
                    # partial failure from a prior run. Re-schedule.
                    log.info(
                        "reminder_scheduler.deadline_unchanged_reschedule",
                        page_id=page_id,
                    )

            # Step 2c: Schedule milestones.
            assigned_slots, enqueue_failures = await schedule_for_task(
                page_id=page_id,
                title=title,
                peer=peer,
                deadline_at=deadline_at,
                urgency=urgency,
                now=now,
                user_tz=_USER_TZ,
            )

            if enqueue_failures:
                log.warning(
                    "reminder_scheduler.partial_failure",
                    page_id=page_id,
                    failed_milestones=enqueue_failures,
                )
                try:
                    await ops_alerts.enqueue(
                        kind="reminder_scheduler_partial_failure",
                        body=(
                            f"reminder_scheduler: partial failure for <page_id>. "
                            f"Failed milestones: {enqueue_failures}. "
                            "Reminder Scheduled At left empty; will retry next nightly run."
                        ),
                        severity="warning",
                    )
                except Exception as alert_exc:
                    log.error(
                        "reminder_scheduler.ops_alert_failed",
                        error=str(alert_exc),
                    )
                failed_count += 1
            elif assigned_slots:
                # Step 2d: All milestones succeeded — mark as scheduled in Notion.
                try:
                    await notion.mark_reminder_scheduled(page_id)
                    scheduled_count += 1
                    log.info(
                        "reminder_scheduler.task_scheduled",
                        page_id=page_id,
                        milestone_count=len(assigned_slots),
                    )
                except Exception as notion_exc:
                    log.warning(
                        "reminder_scheduler.mark_scheduled_failed",
                        page_id=page_id,
                        error=str(notion_exc),
                    )
                    # Outbox rows exist — reminders will fire even if Notion PATCH fails.
                    # The daemon will retry mark_reminder_scheduled next cycle since
                    # Reminder Scheduled At stays empty.
            else:
                # No milestones fit (deadline already passed or too close).
                log.info(
                    "reminder_scheduler.no_milestones",
                    page_id=page_id,
                    deadline_at=deadline_at.isoformat(),
                )
                # Mark as scheduled to stop retrying a deadline in the past.
                try:
                    await notion.mark_reminder_scheduled(page_id)
                except Exception:
                    pass

        except Exception as task_exc:
            log.exception(
                "reminder_scheduler.task_failed",
                page_id=page_id,
                error=str(task_exc),
            )
            try:
                await ops_alerts.enqueue(
                    kind="reminder_scheduler_task_failed",
                    body=(
                        "reminder_scheduler: unexpected error processing <page_id>. "
                        "Will retry on next nightly run."
                    ),
                    severity="warning",
                )
            except Exception:
                pass
            failed_count += 1

    log.info(
        "reminder_scheduler.cycle_done",
        total_tasks=len(tasks),
        scheduled=scheduled_count,
        failed=failed_count,
    )


# ---------------------------------------------------------------------------
# Notion property extraction helpers
# ---------------------------------------------------------------------------

def _extract_title(props: dict[str, Any]) -> str:
    """Extract task title from Notion properties."""
    title_prop = props.get("Title", {})
    title_items = title_prop.get("title", [])
    if title_items:
        return str(title_items[0].get("plain_text", ""))[:200]
    return ""


def _extract_peer(task: dict[str, Any]) -> str:
    """Extract peer (Signal number) from task metadata.

    Notion tasks don't store the peer phone number — it lives in the
    conversation state (Postgres checkpointer). For the daemon, we use
    the AUTHORIZED_PEERS env var (single-tenant: only one user).
    This is the expected single-user design of hide-my-list.
    """
    return os.environ.get("AUTHORIZED_PEERS", "").split(",")[0].strip()


def _extract_deadline(props: dict[str, Any]) -> datetime | None:
    """Extract Due At datetime from Notion properties."""
    due_at_prop = props.get("Due At", {})
    date_obj = due_at_prop.get("date")
    if not date_obj:
        return None
    start = date_obj.get("start")
    if not start:
        return None
    try:
        # ISO 8601 string; normalize 'Z' suffix.
        normalized = start.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (ValueError, TypeError) as exc:
        log.warning("reminder_scheduler.deadline_parse_failed", start=start, error=str(exc))
        return None


def _extract_urgency(props: dict[str, Any]) -> int:
    """Extract urgency from Notion properties with a default of 50."""
    urgency_prop = props.get("Urgency", {})
    value = urgency_prop.get("number")
    if isinstance(value, (int, float)):
        return max(0, min(100, int(value)))
    return 50
