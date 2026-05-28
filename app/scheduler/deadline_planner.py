"""Deadline milestone planner — pure functions, no I/O.

Computes a reminder schedule for a task with a deadline, balancing across
existing scheduled reminders to avoid collisions. Used by:
  - app/scheduler/reminder_scheduling.py: schedule_for_task (called by both
    intake node inline scheduling and reminder_scheduler daemon backstop)

No I/O, no DB, no async. Fully testable without infrastructure.

Env-tunable defaults (read by callers via _env_defaults()):
  REMINDER_SLOT_MINUTES      - time-slot bucket size (default 30)
  REMINDER_SLOT_CAPACITY     - max reminders per slot bucket (default 2)
  REMINDER_QUIET_START_HOUR  - quiet hours start (default 22, i.e. 10pm)
  REMINDER_QUIET_END_HOUR    - quiet hours end   (default 8,  i.e. 8am)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

Tier = Literal["dense", "standard", "sparse"]

# ---------------------------------------------------------------------------
# Tier table
# ---------------------------------------------------------------------------

_TIER_MILESTONES: dict[Tier, list[tuple[str, timedelta]]] = {
    "dense": [
        ("90d", timedelta(days=90)),
        ("30d", timedelta(days=30)),
        ("14d", timedelta(days=14)),
        ("7d",  timedelta(days=7)),
        ("3d",  timedelta(days=3)),
        ("1d",  timedelta(days=1)),
        ("4h",  timedelta(hours=4)),
    ],
    "standard": [
        ("60d", timedelta(days=60)),
        ("14d", timedelta(days=14)),
        ("7d",  timedelta(days=7)),
        ("3d",  timedelta(days=3)),
        ("1d",  timedelta(days=1)),
        ("4h",  timedelta(hours=4)),
    ],
    "sparse": [
        ("60d", timedelta(days=60)),
        ("14d", timedelta(days=14)),
        ("3d",  timedelta(days=3)),
        ("4h",  timedelta(hours=4)),
    ],
}

# For end-of-day (17:00 local) deadlines: preferred local hour for
# multi-day reminder ideal slots.  Sub-day milestones always use
# deadline - offset so they anchor naturally to the clock time.
_EOD_IDEAL_HOUR: dict[str, int] = {
    "90d": 9,
    "60d": 9,
    "30d": 9,
    "14d": 9,
    "7d":  10,
    "3d":  10,
    "1d":  9,
}

# End-of-day sentinel: when deadline local time matches this hour/minute,
# we treat the deadline as "end of day" and anchor ideal slots to morning times.
_EOD_HOUR = 17
_EOD_MINUTE = 0

# Minimum lead time: a milestone is only included when it fires at least
# this far in the future.
_MIN_LEAD = timedelta(minutes=15)


# ---------------------------------------------------------------------------
# MilestoneSlot dataclass
# ---------------------------------------------------------------------------

@dataclass
class MilestoneSlot:
    """A single planned reminder milestone.

    Attributes:
        label:    Human-readable offset string, e.g. "3d", "4h".
        tier:     The tier that generated this milestone.
        ideal_at: The ideal (unloaded) fire time, tz-aware.
    """
    label: str
    tier: Tier
    ideal_at: datetime


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def tier_for(urgency: int) -> Tier:
    """Return the milestone tier for a given urgency score (0-100).

    Args:
        urgency: Integer urgency score. Values outside [0, 100] are accepted
                 but clamped implicitly by the tier thresholds.

    Returns:
        "dense"    when urgency >= 80
        "standard" when 40 <= urgency <= 79
        "sparse"   when urgency < 40
    """
    if urgency >= 80:
        return "dense"
    if urgency >= 40:
        return "standard"
    return "sparse"


def plan_milestones(
    deadline_at: datetime,
    urgency: int,
    now: datetime,
    *,
    user_tz: str = "America/Chicago",
) -> list[MilestoneSlot]:
    """Compute ideal reminder slots for all milestones that fit in remaining time.

    A milestone fits when ``(deadline_at - milestone_offset) > now + 15min``.

    For clock-time deadlines (i.e. the deadline has a specific time other than
    the end-of-day default of 17:00 local), ideal slots are anchored relative
    to the clock time: ``ideal = deadline_at - offset``.

    For end-of-day deadlines (17:00 local), multi-day milestones (>= 1d) use
    a preferred morning hour in user-local time (see _EOD_IDEAL_HOUR), so the
    reminder fires at a sensible time of day rather than 5pm-minus-offset.
    Sub-day milestones (4h) always use ``deadline - 4h`` regardless.

    Args:
        deadline_at: Deadline datetime, must be tz-aware.
        urgency:     Integer urgency score used to select the tier.
        now:         Current time, must be tz-aware.
        user_tz:     IANA timezone string for the user.

    Returns:
        List of MilestoneSlot, sorted by ideal_at ascending (earliest first).
    """
    tz = ZoneInfo(user_tz)
    tier = tier_for(urgency)
    milestones = _TIER_MILESTONES[tier]

    # Determine whether the deadline is an end-of-day deadline.
    deadline_local = deadline_at.astimezone(tz)
    is_eod = (deadline_local.hour == _EOD_HOUR and deadline_local.minute == _EOD_MINUTE)

    slots: list[MilestoneSlot] = []
    for label, offset in milestones:
        raw_ideal = deadline_at - offset

        # Skip milestones that don't leave at least 15 min of lead time.
        if raw_ideal <= now + _MIN_LEAD:
            continue

        # Determine the ideal fire time.
        if is_eod and label in _EOD_IDEAL_HOUR:
            # Anchor to a preferred morning hour on the milestone date.
            milestone_date_local = raw_ideal.astimezone(tz).date()
            preferred_hour = _EOD_IDEAL_HOUR[label]
            ideal = datetime(
                milestone_date_local.year,
                milestone_date_local.month,
                milestone_date_local.day,
                preferred_hour,
                0,
                0,
                tzinfo=tz,
            )
            # Safety: if the anchored time is in the past or too close to now,
            # fall back to raw_ideal (offset-anchored).
            if ideal <= now + _MIN_LEAD:
                ideal = raw_ideal
        else:
            ideal = raw_ideal

        slots.append(MilestoneSlot(label=label, tier=tier, ideal_at=ideal))

    slots.sort(key=lambda s: s.ideal_at)
    return slots


def assign_slot(
    ideal_slot: datetime,
    existing_slots: list[datetime],
    deadline_at: datetime,
    *,
    user_tz: str = "America/Chicago",
    quiet_hours: tuple[int, int] = (22, 8),
    slot_minutes: int = 30,
    slot_capacity: int = 2,
    max_drift_minutes: int = 720,
    now: datetime | None = None,
) -> tuple[datetime, bool]:
    """Find an available slot near the ideal time.

    Walks outward from the ideal slot in ``slot_minutes`` increments, trying
    earlier times first (never in the past, never past deadline), skipping
    quiet hours and over-capacity buckets.  Falls back to the ideal if no
    open slot is found within ``max_drift_minutes``.

    Args:
        ideal_slot:       Desired fire time (tz-aware).
        existing_slots:   Already-assigned reminder datetimes in the window.
        deadline_at:      Task deadline (tz-aware); result must be < deadline_at.
        user_tz:          IANA timezone for quiet-hours evaluation.
        quiet_hours:      ``(start_hour, end_hour)`` in user-local time.
                          Times in [start_hour, 24) ∪ [0, end_hour) are quiet.
        slot_minutes:     Bucket granularity in minutes.
        slot_capacity:    Maximum allowed reminders per bucket.
        max_drift_minutes: Search radius around the ideal slot.
        now:              "Never fire in past" cutoff.  Defaults to
                          ``deadline_at - timedelta(hours=1)`` when None.

    Returns:
        ``(chosen_slot, used_fallback)`` where ``used_fallback`` is True when
        no open slot was found and the caller should emit an ops_alert.
    """
    if now is None:
        now = deadline_at - timedelta(hours=1)

    tz = ZoneInfo(user_tz)
    q_start, q_end = quiet_hours
    step = timedelta(minutes=slot_minutes)
    max_drift = timedelta(minutes=max_drift_minutes)

    # Snap ideal_slot to nearest bucket boundary.
    snapped_ideal = _snap_to_bucket(ideal_slot, slot_minutes)

    def _bucket_key(dt: datetime) -> datetime:
        return _snap_to_bucket(dt, slot_minutes)

    def _occupancy(candidate: datetime) -> int:
        key = _bucket_key(candidate)
        return sum(1 for s in existing_slots if _bucket_key(s) == key)

    def _in_quiet(candidate: datetime) -> bool:
        local_h = candidate.astimezone(tz).hour
        if q_start < q_end:
            # Quiet window doesn't cross midnight
            return q_start <= local_h < q_end
        else:
            # Crosses midnight: quiet when hour >= q_start OR hour < q_end
            return local_h >= q_start or local_h < q_end

    def _is_valid(candidate: datetime) -> bool:
        """Candidate is valid if: > now+15min, < deadline, not quiet, not full."""
        if candidate <= now + _MIN_LEAD:
            return False
        if candidate >= deadline_at:
            return False
        if _in_quiet(candidate):
            return False
        if _occupancy(candidate) >= slot_capacity:
            return False
        return True

    if _is_valid(snapped_ideal):
        return snapped_ideal, False

    # Walk outward from snapped_ideal: try earlier steps (i=1,2,...) before
    # the corresponding later step.  Stop when both sides exhaust max_drift.
    steps = int(max_drift / step)
    for i in range(1, steps + 1):
        earlier = snapped_ideal - step * i
        later   = snapped_ideal + step * i

        if earlier >= snapped_ideal - max_drift:
            if _is_valid(earlier):
                return earlier, False

        if later <= snapped_ideal + max_drift:
            if _is_valid(later):
                return later, False

    # Nothing found within max_drift — return ideal with fallback flag.
    return ideal_slot, True


def format_reminder_summary(
    slots: list[tuple[str, datetime]],
    user_tz: str = "America/Chicago",
) -> str:
    """Format a human-readable reminder summary for task confirmations.

    Slots are sorted by datetime ascending before formatting so the summary
    reads chronologically regardless of the order they were passed in.

    Args:
        slots:    List of ``(milestone_label, assigned_slot)`` pairs.
        user_tz:  IANA timezone for display.

    Returns:
        A sentence like ``"I'll ping you Wed noon, Thu 9am."``
        Returns empty string when ``slots`` is empty.

    Examples:
        >>> format_reminder_summary([("3d", wed_noon), ("1d", thu_9am)], "America/Chicago")
        "I'll ping you Wed noon, Thu 9am."
        >>> format_reminder_summary([], "America/Chicago")
        ""
    """
    if not slots:
        return ""

    # Sort chronologically ascending before formatting.
    sorted_slots = sorted(slots, key=lambda pair: pair[1])

    tz = ZoneInfo(user_tz)
    parts: list[str] = []
    for _label, dt in sorted_slots:
        local = dt.astimezone(tz)
        parts.append(_format_datetime(local))

    if len(parts) == 1:
        return f"I'll ping you {parts[0]}."
    return "I'll ping you " + ", ".join(parts) + "."


# ---------------------------------------------------------------------------
# Env defaults helper (callers do the env read; pure-function discipline)
# ---------------------------------------------------------------------------

def _env_defaults() -> dict[str, int | tuple[int, int]]:
    """Read reminder scheduling defaults from environment variables.

    Returns a dict of kwargs suitable for passing to assign_slot:
      slot_minutes, slot_capacity, quiet_hours.

    Callers pass these as **kwargs; assign_slot itself takes them as plain
    parameters so it remains a pure function.

    Example::

        defaults = _env_defaults()
        slot, fallback = assign_slot(ideal, existing, deadline, **defaults)
    """
    slot_minutes   = int(os.environ.get("REMINDER_SLOT_MINUTES",   "30"))
    slot_capacity  = int(os.environ.get("REMINDER_SLOT_CAPACITY",  "2"))
    quiet_start    = int(os.environ.get("REMINDER_QUIET_START_HOUR", "22"))
    quiet_end      = int(os.environ.get("REMINDER_QUIET_END_HOUR",   "8"))
    return {
        "slot_minutes":  slot_minutes,
        "slot_capacity": slot_capacity,
        "quiet_hours":   (quiet_start, quiet_end),
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _snap_to_bucket(dt: datetime, slot_minutes: int) -> datetime:
    """Round dt down to the nearest slot_minutes boundary (UTC-based)."""
    total_minutes = int(dt.timestamp() // 60)
    snapped_minutes = (total_minutes // slot_minutes) * slot_minutes
    return datetime.fromtimestamp(snapped_minutes * 60, tz=dt.tzinfo)


def _format_datetime(local: datetime) -> str:
    """Format a local datetime as a natural-language string.

    Examples:
        12:00 -> "noon"
        00:00 -> "midnight"
        08:00 -> "8am"
        13:00 -> "1pm"
        09:30 -> "9:30am"
        22:30 -> "10:30pm"
    """
    h = local.hour
    m = local.minute
    day = local.strftime("%a")  # e.g. "Wed"

    if h == 12 and m == 0:
        return f"{day} noon"
    if h == 0 and m == 0:
        return f"{day} midnight"

    # 12-hour format
    if h == 0:
        period = "am"
        display_h = 12
    elif h < 12:
        period = "am"
        display_h = h
    elif h == 12:
        period = "pm"
        display_h = 12
    else:
        period = "pm"
        display_h = h - 12

    if m == 0:
        return f"{day} {display_h}{period}"
    return f"{day} {display_h}:{m:02d}{period}"
