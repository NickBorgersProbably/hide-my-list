"""Declarative APScheduler job list for hide-my-list.

SCHEDULED_JOBS is the single source of truth for all scheduled jobs.
reconcile_jobstore() enforces it: orphaned jobs (in DB but not in this list)
are removed, and declared jobs are added/updated with replace_existing=True.

Orphan removal is critical: APScheduler's replace_existing=True only updates
existing jobs; it does NOT remove jobs that were deleted from SCHEDULED_JOBS.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import structlog
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

log = structlog.get_logger(__name__)

_USER_TZ = os.environ.get("USER_TZ", "America/Chicago")


@dataclass
class JobSpec:
    """Spec for a single APScheduler job."""
    id: str
    trigger: Any
    func: Any
    kwargs: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Job implementations
# ---------------------------------------------------------------------------


async def dispatch_due_reminders() -> None:
    """Invoke reminder_worker to deliver all due outbox items."""
    from app.scheduler.reminder_worker import run_worker_once
    await run_worker_once()


async def check_notion_health() -> None:
    """Ping Notion API and emit an ops alert on failure."""
    from app.tools import notion
    try:
        # A lightweight probe: query the database with a limit-0 filter.
        # We can't do a true zero-result query, but a small query is cheap.
        await notion.query_all()
        log.info("notion_health.ok")
    except Exception as exc:
        log.error("notion_health.failed", error=str(exc))
        # Phase C will implement real ops alert delivery.
        # For now, structured logging is the signal.


async def send_pending_ops_alerts() -> None:
    """Drain ops alerts queue and deliver any pending alerts.

    # Real impl in Phase C
    """
    log.debug("ops_alerts_drain.tick")


async def generate_weekly_recap() -> None:
    """Generate and send weekly recap to the user.

    # Real impl in Phase B/C
    """
    log.debug("weekly_recap.tick")


# ---------------------------------------------------------------------------
# Declarative job list — single source of truth
# ---------------------------------------------------------------------------

SCHEDULED_JOBS: list[JobSpec] = [
    JobSpec(
        id="reminder_dispatcher",
        trigger=IntervalTrigger(seconds=30),
        func=dispatch_due_reminders,
    ),
    JobSpec(
        id="notion_health",
        trigger=IntervalTrigger(minutes=15),
        func=check_notion_health,
    ),
    JobSpec(
        id="ops_alerts_drain",
        trigger=IntervalTrigger(minutes=5),
        func=send_pending_ops_alerts,
    ),
    JobSpec(
        id="weekly_recap",
        trigger=CronTrigger(
            day_of_week="sun",
            hour=18,
            timezone=_USER_TZ,
        ),
        func=generate_weekly_recap,
    ),
]


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def reconcile_jobstore(scheduler: Any) -> None:
    """Enforce SCHEDULED_JOBS against the APScheduler jobstore.

    1. Remove any jobs persisted in the jobstore that are NOT in SCHEDULED_JOBS
       (orphan removal — replace_existing alone does not remove deleted jobs).
    2. Add/update all declared jobs with replace_existing=True.

    Args:
        scheduler: A running APScheduler BlockingScheduler or AsyncIOScheduler.
    """
    declared_ids = {j.id for j in SCHEDULED_JOBS}

    # Step 1: Remove orphans
    for existing_job in scheduler.get_jobs():
        if existing_job.id not in declared_ids:
            log.info("reconcile_jobstore.removing_orphan", job_id=existing_job.id)
            scheduler.remove_job(existing_job.id)

    # Step 2: Add/update declared jobs
    for spec in SCHEDULED_JOBS:
        kwargs = spec.kwargs or {}
        scheduler.add_job(
            spec.func,
            spec.trigger,
            id=spec.id,
            replace_existing=True,
            **kwargs,
        )
        log.info("reconcile_jobstore.registered", job_id=spec.id)
