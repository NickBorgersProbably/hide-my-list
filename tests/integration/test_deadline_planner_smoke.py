"""Integration smoke test for app.scheduler.deadline_planner.

Exercises the full public API surface in an integration-shaped manner:
computes a milestone plan, assigns slots for each milestone, and verifies
no collisions emerge.  No DB or I/O — "integration" here means the functions
compose correctly end-to-end, satisfying test-rig clause 1 for new public
functions.

Reachability for the DB-touching callers is in:
  - tests/integration/test_intake_deadlines.py (inline scheduling path)
  - tests/integration/test_reminder_scheduler.py (daemon path)

Private data: all values use placeholders / synthetic datetimes.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.scheduler.deadline_planner import (
    assign_slot,
    format_reminder_summary,
    plan_milestones,
    tier_for,
)

_TZ_STR = "America/Chicago"
_TZ = ZoneInfo(_TZ_STR)


def _local(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=_TZ)


class TestDeadlinePlannerSmoke:
    """End-to-end composition: plan → assign → summarise → no collisions."""

    def test_dense_plan_assign_no_collisions(self) -> None:
        """Dense tier: plan all 7 milestones, assign slots, assert no bucket collisions."""
        now = _local(2026, 1, 1, 10, 0)
        deadline = _local(2026, 7, 20, 17, 0)   # ~200 days away

        assert tier_for(85) == "dense"
        milestones = plan_milestones(deadline, urgency=85, now=now, user_tz=_TZ_STR)
        assert len(milestones) == 7

        assigned: list[datetime] = []
        fallback_count = 0
        for m in milestones:
            slot, used_fallback = assign_slot(
                m.ideal_at,
                assigned,
                deadline,
                user_tz=_TZ_STR,
                quiet_hours=(22, 8),
                slot_minutes=30,
                slot_capacity=2,
                max_drift_minutes=720,
                now=now,
            )
            if used_fallback:
                fallback_count += 1
            assigned.append(slot)
            # Slot must be before deadline
            assert slot < deadline
            # Slot must be after now (with 15-min lead)
            assert slot > now + timedelta(minutes=15)

        # All 7 milestones spaced far apart → no fallbacks expected
        assert fallback_count == 0, f"Unexpected fallbacks: {fallback_count}"

    def test_standard_plan_assign_summary_roundtrip(self) -> None:
        """Standard tier: plan → assign → format_reminder_summary roundtrip."""
        now = _local(2026, 6, 1, 10, 0)
        deadline = _local(2026, 6, 30, 17, 0)   # 29 days away

        milestones = plan_milestones(deadline, urgency=60, now=now, user_tz=_TZ_STR)
        assert len(milestones) > 0

        assigned: list[tuple[str, datetime]] = []
        for m in milestones:
            slot, _ = assign_slot(
                m.ideal_at,
                [s for _, s in assigned],
                deadline,
                user_tz=_TZ_STR,
                quiet_hours=(22, 8),
                slot_minutes=30,
                slot_capacity=2,
                max_drift_minutes=720,
                now=now,
            )
            assigned.append((m.label, slot))

        summary = format_reminder_summary(assigned, user_tz=_TZ_STR)
        assert summary.startswith("I'll ping you ")
        assert summary.endswith(".")

    def test_summary_is_chronologically_sorted(self) -> None:
        """format_reminder_summary output is always chronologically ascending."""
        now = _local(2026, 6, 1, 10, 0)
        deadline = _local(2026, 6, 30, 17, 0)

        milestones = plan_milestones(deadline, urgency=60, now=now, user_tz=_TZ_STR)
        assigned: list[tuple[str, datetime]] = []
        for m in milestones:
            slot, _ = assign_slot(
                m.ideal_at,
                [s for _, s in assigned],
                deadline,
                user_tz=_TZ_STR,
                quiet_hours=(22, 8),
                slot_minutes=30,
                slot_capacity=2,
                max_drift_minutes=720,
                now=now,
            )
            assigned.append((m.label, slot))

        # Reverse the order before passing to format_reminder_summary
        # to ensure it re-sorts regardless of input order.
        reversed_slots = list(reversed(assigned))
        summary_forward = format_reminder_summary(assigned, user_tz=_TZ_STR)
        summary_reversed = format_reminder_summary(reversed_slots, user_tz=_TZ_STR)
        # Both must produce the same (chronological) output
        assert summary_forward == summary_reversed

    def test_sparse_short_deadline_single_reminder(self) -> None:
        """Sparse tier + 2-day deadline → exactly 1 milestone (4h), valid slot."""
        now = _local(2026, 6, 1, 10, 0)
        deadline = _local(2026, 6, 3, 17, 0)   # 55h away

        milestones = plan_milestones(deadline, urgency=20, now=now, user_tz=_TZ_STR)
        assert len(milestones) == 1
        assert milestones[0].label == "4h"

        slot, fallback = assign_slot(
            milestones[0].ideal_at,
            [],
            deadline,
            user_tz=_TZ_STR,
            quiet_hours=(22, 8),
            slot_minutes=30,
            slot_capacity=2,
            max_drift_minutes=720,
            now=now,
        )
        assert not fallback
        assert slot < deadline
        assert slot > now + timedelta(minutes=15)

    def test_10_tasks_assign_no_slot_overflow(self) -> None:
        """Simulate 10 tasks with 3d deadline each; assign all slots; bucket occupancy <= capacity."""
        now = _local(2026, 6, 1, 10, 0)
        deadline = _local(2026, 6, 4, 17, 0)   # 3 days away
        capacity = 2

        all_slots: list[datetime] = []
        for task_i in range(10):
            # Stagger urgency so we get a mix of sparse/standard
            urgency = 30 + task_i * 5   # 30..75
            milestones = plan_milestones(deadline, urgency=urgency, now=now, user_tz=_TZ_STR)
            for m in milestones:
                slot, _ = assign_slot(
                    m.ideal_at,
                    all_slots,
                    deadline,
                    user_tz=_TZ_STR,
                    quiet_hours=(22, 8),
                    slot_minutes=30,
                    slot_capacity=capacity,
                    max_drift_minutes=720,
                    now=now,
                )
                all_slots.append(slot)

        # Count occupancy per 30-min bucket
        from collections import Counter

        def _bucket(dt: datetime) -> int:
            return int(dt.timestamp()) // (30 * 60)

        counts = Counter(_bucket(s) for s in all_slots)
        overflow = {k: v for k, v in counts.items() if v > capacity}
        assert not overflow, f"Bucket(s) exceed capacity={capacity}: {overflow}"

    def test_format_empty_returns_empty(self) -> None:
        """Empty milestone list → empty summary string."""
        now = _local(2026, 6, 10, 12, 0)
        deadline = _local(2026, 6, 5, 17, 0)   # past deadline → no milestones
        milestones = plan_milestones(deadline, urgency=80, now=now, user_tz=_TZ_STR)
        assert milestones == []
        summary = format_reminder_summary([], user_tz=_TZ_STR)
        assert summary == ""

    def test_quiet_hours_respected_across_all_assignments(self) -> None:
        """No assigned slot falls in quiet hours (22:00-08:00 local)."""
        now = _local(2026, 1, 1, 10, 0)
        deadline = _local(2026, 7, 20, 17, 0)

        milestones = plan_milestones(deadline, urgency=85, now=now, user_tz=_TZ_STR)
        assigned: list[datetime] = []
        for m in milestones:
            slot, _ = assign_slot(
                m.ideal_at,
                assigned,
                deadline,
                user_tz=_TZ_STR,
                quiet_hours=(22, 8),
                slot_minutes=30,
                slot_capacity=2,
                max_drift_minutes=720,
                now=now,
            )
            assigned.append(slot)

        for slot in assigned:
            local_h = slot.astimezone(_TZ).hour
            # Not in quiet window [22, 24) ∪ [0, 8)
            assert 8 <= local_h < 22, (
                f"Slot {slot.astimezone(_TZ).isoformat()} falls in quiet hours (hour={local_h})"
            )
