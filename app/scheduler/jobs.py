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
    log.debug("dispatch_due_reminders.tick")
    from app.scheduler.reminder_worker import run_worker_once
    await run_worker_once()


async def check_notion_health() -> None:
    """Ping Notion API and enqueue an ops alert on failure.

    Replacement for OpenClaw heartbeat Check 3 (Notion connectivity).
    Runs every 15 minutes. Calls notion.health_check() which GETs /v1/users/me.
    On failure, enqueues a 'notion_health_failed' ops alert; the drain job
    delivers it to OPS_ALERT_SIGNAL_NUMBER. Throttled to avoid alert storms.
    """
    from app.tools import notion, ops_alerts

    ok = await notion.health_check()
    if not ok:
        try:
            await ops_alerts.enqueue(
                kind="notion_health_failed",
                body="Notion API health check failed. Verify NOTION_API_KEY is valid and api.notion.com is reachable.",
                severity="critical",
            )
        except Exception as exc:
            # If DB is also down, log and continue — don't crash the job.
            log.error("notion_health.enqueue_alert_failed", error=str(exc))


async def send_pending_ops_alerts() -> None:
    """Drain ops alerts queue and deliver any pending alerts via Signal.

    Replacement for OpenClaw heartbeat alert dispatch path.
    Runs every 5 minutes. Calls ops_alerts.drain() which:
      - Fetches pending alerts from the ops_alerts Postgres table.
      - Sends each via signal_client to OPS_ALERT_SIGNAL_NUMBER.
      - Marks delivered; updates per-kind throttle.
    """
    from app.tools import ops_alerts
    await ops_alerts.drain()


async def run_state_audit() -> None:
    """Nightly database maintenance: VACUUM + retention pruning.

    Replacement for OpenClaw heartbeat Check 6 (memory/state audit).
    Runs nightly at 3am user time (USER_TZ env var).

    Operations:
      1. VACUUM on the Postgres database (reclaims dead tuple space).
      2. Delete recent_outbound rows older than 90 days.

    Idempotent: safe to re-run. Rows already pruned are not re-deleted.
    Only runs when ENABLE_LANGGRAPH_PATH=true; logs a dormancy message otherwise.
    """
    _enabled = os.environ.get("ENABLE_LANGGRAPH_PATH", "true").lower() in ("true", "1", "yes")
    if not _enabled:
        log.debug("state_audit.dormant")
        return

    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from app.tools.db import get_db_conn

    now = datetime.now(UTC)
    recent_outbound_cutoff = now - timedelta(days=90)

    try:
        async with get_db_conn() as conn:
            # VACUUM must run outside a transaction block.
            # psycopg async connections don't support autocommit switch
            # after connection; we use a direct execute with AUTOCOMMIT.
            old_autocommit = conn.autocommit
            await conn.set_autocommit(True)
            await conn.execute("VACUUM")
            await conn.set_autocommit(old_autocommit)
            log.info("state_audit.vacuum.done")

        async with get_db_conn() as conn:
            result = await conn.execute(
                "DELETE FROM recent_outbound WHERE expires_at < %s",
                (recent_outbound_cutoff,),
            )
            deleted_outbound = result.rowcount
            log.info(
                "state_audit.recent_outbound.pruned",
                deleted=deleted_outbound,
                cutoff=recent_outbound_cutoff.isoformat(),
            )

    except Exception as exc:
        log.error("state_audit.failed", error=str(exc))
        # Enqueue ops alert so operator knows audit failed.
        try:
            from app.tools import ops_alerts
            await ops_alerts.enqueue(
                kind="state_audit_failed",
                body=f"Nightly state audit failed: {exc}",
                severity="warning",
            )
        except Exception as inner_exc:
            log.error("state_audit.enqueue_alert_failed", error=str(inner_exc))


async def generate_weekly_recap() -> None:
    """Generate and send weekly recap to the user.

    # Real impl in Phase B/C
    """
    log.debug("weekly_recap.tick")


async def dispatch_check_ins() -> None:
    """Find peers with active tasks past their check-in window and invoke CHECK_IN.

    Fires every 10 minutes. For each peer with:
      - conversation_state = "active"
      - active_task.check_in_due_at <= now

    Invokes the graph with intent=CHECK_IN so the check_in node runs through
    shared conversation history (per the graph's checkpointer).

    This job does NOT classify from user messages — it injects CHECK_IN directly
    via the graph's routing (routing.check_in_route).
    """
    _enabled = os.environ.get("ENABLE_LANGGRAPH_PATH", "true").lower() in ("true", "1", "yes")
    if not _enabled:
        log.debug("dispatch_check_ins.dormant")
        return

    # Phase B implementation: stub that logs intent.
    # Phase C wires this to query active tasks from Postgres/Notion and invoke graph.
    log.debug("dispatch_check_ins.tick")


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
        id="state_audit",
        trigger=CronTrigger(
            hour=3,
            minute=0,
            timezone=_USER_TZ,
        ),
        func=run_state_audit,
    ),
    JobSpec(
        id="check_in_dispatcher",
        trigger=IntervalTrigger(minutes=10),
        func=dispatch_check_ins,
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
