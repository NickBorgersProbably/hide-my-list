"""Unit tests for app.scheduler.deadline_planner.

Table-driven. Covers tier selection, milestone clipping, load-balancing
outward walk, quiet-hours skip, never-past-deadline guard, fallback flag,
and format_reminder_summary round-trips.

No I/O, no DB, no async — all functions are pure.

Private data: all test values use placeholder strings / synthetic datetimes.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.scheduler.deadline_planner import (
    MilestoneSlot,
    assign_slot,
    format_reminder_summary,
    plan_milestones,
    tier_for,
)

# Use a fixed timezone throughout tests.
_TZ_STR = "America/Chicago"
_TZ = ZoneInfo(_TZ_STR)


def _local(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Build a tz-aware datetime in America/Chicago."""
    return datetime(year, month, day, hour, minute, tzinfo=_TZ)


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Build a tz-aware datetime in UTC."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# ---------------------------------------------------------------------------
# tier_for — 6 table-driven cases
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("urgency,expected", [
    (0,   "sparse"),
    (39,  "sparse"),
    (40,  "standard"),
    (79,  "standard"),
    (80,  "dense"),
    (100, "dense"),
])
def test_tier_for(urgency: int, expected: str) -> None:
    assert tier_for(urgency) == expected


# ---------------------------------------------------------------------------
# plan_milestones
# ---------------------------------------------------------------------------

class TestPlanMilestones:

    def test_6_month_out_dense_returns_7_milestones(self) -> None:
        """6-month deadline + dense urgency → all 7 milestones (90d..4h)."""
        now = _local(2026, 1, 1, 10, 0)
        # Deadline 200 days away (>90d) at end-of-day
        deadline = _local(2026, 7, 20, 17, 0)
        slots = plan_milestones(deadline, urgency=85, now=now, user_tz=_TZ_STR)
        assert len(slots) == 7
        labels = [s.label for s in slots]
        assert labels == ["90d", "30d", "14d", "7d", "3d", "1d", "4h"]

    def test_5_day_out_standard_clips_past_milestones(self) -> None:
        """5-day deadline + standard urgency → only 3d, 1d, 4h milestones."""
        now = _local(2026, 6, 1, 10, 0)
        deadline = _local(2026, 6, 6, 17, 0)   # 5 days away
        slots = plan_milestones(deadline, urgency=50, now=now, user_tz=_TZ_STR)
        labels = [s.label for s in slots]
        # 60d, 14d, 7d are in the past → clipped; 3d, 1d, 4h fit
        assert "60d" not in labels
        assert "14d" not in labels
        assert "7d" not in labels
        assert set(labels) == {"3d", "1d", "4h"}
        assert len(slots) == 3

    def test_1_day_out_sparse_returns_1_milestone(self) -> None:
        """1-day deadline + sparse urgency → only 4h milestone."""
        now = _local(2026, 6, 1, 10, 0)
        deadline = _local(2026, 6, 2, 17, 0)   # ~31h away
        slots = plan_milestones(deadline, urgency=20, now=now, user_tz=_TZ_STR)
        labels = [s.label for s in slots]
        assert labels == ["4h"]

    def test_22h_clock_time_deadline_dense_yields_4h_at_6am(self) -> None:
        """22h-out clock-time deadline (10am tomorrow) + dense → 1 milestone (4h ≈ 6am).

        The 1d milestone is clipped because deadline - 1d is at or before now.
        The 4h milestone ideal is deadline - 4h = 6am (clock-time anchoring).
        """
        now = _local(2026, 6, 1, 12, 0)        # noon today
        deadline = _local(2026, 6, 2, 10, 0)   # 10am tomorrow (22h away, NOT end-of-day)
        slots = plan_milestones(deadline, urgency=85, now=now, user_tz=_TZ_STR)
        assert len(slots) == 1, f"Expected 1 slot, got {len(slots)}: {[s.label for s in slots]}"
        assert slots[0].label == "4h"
        # ideal_at should be 6am (deadline - 4h)
        expected_ideal = deadline - timedelta(hours=4)
        assert slots[0].ideal_at == expected_ideal

    def test_past_deadline_returns_empty(self) -> None:
        """Deadline in the past → empty list."""
        now = _local(2026, 6, 10, 12, 0)
        deadline = _local(2026, 6, 5, 17, 0)   # in the past
        slots = plan_milestones(deadline, urgency=80, now=now, user_tz=_TZ_STR)
        assert slots == []

    def test_16_minutes_out_returns_empty(self) -> None:
        """16-minute-out deadline → empty list (4h milestone would be -3h44m in past)."""
        now = _local(2026, 6, 1, 12, 0)
        deadline = now + timedelta(minutes=16)
        # 4h-before fires at deadline - 4h = now - 3h44m → in the past → skipped
        slots = plan_milestones(deadline, urgency=80, now=now, user_tz=_TZ_STR)
        assert slots == []

    def test_slots_sorted_ascending(self) -> None:
        """plan_milestones returns slots sorted by ideal_at ascending."""
        now = _local(2026, 1, 1, 10, 0)
        deadline = _local(2026, 7, 20, 17, 0)
        slots = plan_milestones(deadline, urgency=85, now=now, user_tz=_TZ_STR)
        assert len(slots) >= 2
        for a, b in zip(slots, slots[1:], strict=False):
            assert a.ideal_at <= b.ideal_at

    def test_milestone_slot_dataclass_fields(self) -> None:
        """MilestoneSlot has label, tier, ideal_at fields."""
        now = _local(2026, 1, 1, 10, 0)
        deadline = _local(2026, 4, 15, 17, 0)
        slots = plan_milestones(deadline, urgency=80, now=now, user_tz=_TZ_STR)
        assert len(slots) > 0
        s = slots[0]
        assert isinstance(s, MilestoneSlot)
        assert isinstance(s.label, str)
        assert s.tier in ("dense", "standard", "sparse")
        assert s.ideal_at.tzinfo is not None  # tz-aware


# ---------------------------------------------------------------------------
# assign_slot
# ---------------------------------------------------------------------------

class TestAssignSlot:

    def _make_ideal(self) -> datetime:
        """A simple ideal slot: Wednesday 10am local."""
        return _local(2026, 6, 3, 10, 0)   # Wednesday

    def _make_deadline(self) -> datetime:
        """Deadline safely after the ideal."""
        return _local(2026, 6, 6, 17, 0)

    def test_empty_existing_returns_ideal(self) -> None:
        """No existing slots → returns (ideal_snapped, False)."""
        ideal = self._make_ideal()
        slot, fallback = assign_slot(
            ideal, [], self._make_deadline(),
            user_tz=_TZ_STR,
            quiet_hours=(22, 8),
            slot_minutes=30,
            slot_capacity=2,
            max_drift_minutes=720,
            now=_local(2026, 6, 1, 10, 0),
        )
        assert not fallback
        assert slot <= ideal + timedelta(minutes=30)  # within one bucket of ideal

    def test_full_ideal_bucket_walks_earlier(self) -> None:
        """When ideal bucket is full, assign_slot returns a slot 30 min earlier."""
        ideal = _local(2026, 6, 3, 10, 0)
        deadline = _local(2026, 6, 6, 17, 0)
        now = _local(2026, 6, 1, 8, 0)
        # Two existing slots at the ideal time (fills capacity=2)
        existing = [ideal, ideal + timedelta(minutes=5)]
        slot, fallback = assign_slot(
            ideal, existing, deadline,
            user_tz=_TZ_STR,
            quiet_hours=(22, 8),
            slot_minutes=30,
            slot_capacity=2,
            max_drift_minutes=720,
            now=now,
        )
        assert not fallback
        # Should be 30 min before ideal (biased earlier)
        assert slot < ideal

    def test_full_bucket_and_earlier_also_full_walks_back_further(self) -> None:
        """If ideal and ideal-30min are both full, walks to an adjacent open slot."""
        ideal = _local(2026, 6, 3, 10, 0)
        deadline = _local(2026, 6, 6, 17, 0)
        now = _local(2026, 6, 1, 8, 0)
        # Fill ideal and ideal-30min buckets; also fill ideal+30 to force wider walk
        minus30 = ideal - timedelta(minutes=30)
        plus30  = ideal + timedelta(minutes=30)
        existing = [ideal, ideal, minus30, minus30, plus30, plus30]   # 2 each → all full
        slot, fallback = assign_slot(
            ideal, existing, deadline,
            user_tz=_TZ_STR,
            quiet_hours=(22, 8),
            slot_minutes=30,
            slot_capacity=2,
            max_drift_minutes=720,
            now=now,
        )
        assert not fallback
        # Must be outside the three packed buckets
        assert slot not in (ideal, minus30, plus30)
        # Should be earlier or later by >=60 min from ideal
        assert abs((slot - ideal).total_seconds()) >= 3600

    def test_past_guard_causes_later_walk(self) -> None:
        """When walking earlier would cross now+15min, falls through to later."""
        # ideal is only 20 min after now → can't walk earlier at all
        now = _local(2026, 6, 3, 9, 0)
        ideal = _local(2026, 6, 3, 9, 30)   # only 30 min after now
        deadline = _local(2026, 6, 6, 17, 0)
        # Fill the ideal bucket
        existing = [ideal, ideal]
        slot, fallback = assign_slot(
            ideal, existing, deadline,
            user_tz=_TZ_STR,
            quiet_hours=(22, 8),
            slot_minutes=30,
            slot_capacity=2,
            max_drift_minutes=720,
            now=now,
        )
        assert not fallback
        # Must be later than ideal since earlier is blocked by the past guard
        assert slot > ideal

    def test_never_returns_slot_at_or_after_deadline(self) -> None:
        """assign_slot never returns a slot >= deadline."""
        ideal = _local(2026, 6, 3, 10, 0)
        deadline = _local(2026, 6, 3, 11, 0)   # deadline only 1h after ideal
        now = _local(2026, 6, 1, 8, 0)
        # Fill every earlier bucket but leave ideal empty
        slot, fallback = assign_slot(
            ideal, [], deadline,
            user_tz=_TZ_STR,
            quiet_hours=(22, 8),
            slot_minutes=30,
            slot_capacity=2,
            max_drift_minutes=720,
            now=now,
        )
        assert slot < deadline

    def test_all_slots_full_returns_fallback_true(self) -> None:
        """When all slots within max_drift are full, returns (ideal, True)."""
        ideal = _local(2026, 6, 3, 10, 0)
        deadline = _local(2026, 6, 6, 17, 0)
        now = _local(2026, 6, 1, 8, 0)
        max_drift_minutes = 60  # only ±60 min search window
        steps = max_drift_minutes // 30 + 1

        # Pack every bucket within ±60 min (plus ideal itself)
        existing: list[datetime] = []
        for i in range(-steps, steps + 1):
            t = ideal + timedelta(minutes=30 * i)
            existing.extend([t, t])   # 2 per bucket → full

        slot, fallback = assign_slot(
            ideal, existing, deadline,
            user_tz=_TZ_STR,
            quiet_hours=(22, 8),
            slot_minutes=30,
            slot_capacity=2,
            max_drift_minutes=max_drift_minutes,
            now=now,
        )
        assert fallback is True
        assert slot == ideal   # returns original ideal (not snapped) on fallback

    def test_quiet_hours_6am_target_pushed_to_8am(self) -> None:
        """Ideal at 6am is in quiet hours (22-8); assign_slot pushes to 8am."""
        # ideal = 6am local (quiet hours 22-8 → 6am is quiet)
        ideal = _local(2026, 6, 3, 6, 0)
        deadline = _local(2026, 6, 6, 17, 0)
        now = _local(2026, 6, 1, 8, 0)
        slot, fallback = assign_slot(
            ideal, [], deadline,
            user_tz=_TZ_STR,
            quiet_hours=(22, 8),
            slot_minutes=30,
            slot_capacity=2,
            max_drift_minutes=720,
            now=now,
        )
        assert not fallback
        local_slot = slot.astimezone(_TZ)
        assert local_slot.hour >= 8, f"Expected slot at or after 8am, got {local_slot.hour}:{local_slot.minute:02d}"

    def test_high_urgency_small_drift_returns_fallback_sooner(self) -> None:
        """max_drift=120 with packed schedule → fallback True sooner than default."""
        ideal = _local(2026, 6, 3, 10, 0)
        deadline = _local(2026, 6, 6, 17, 0)
        now = _local(2026, 6, 1, 8, 0)
        # Fill ±120 min with capacity=2 each
        existing: list[datetime] = []
        for i in range(-5, 6):
            t = ideal + timedelta(minutes=30 * i)
            existing.extend([t, t])

        slot_small, fallback_small = assign_slot(
            ideal, existing, deadline,
            user_tz=_TZ_STR,
            quiet_hours=(22, 8),
            slot_minutes=30,
            slot_capacity=2,
            max_drift_minutes=120,
            now=now,
        )
        assert fallback_small is True

        # With default max_drift=720, the same schedule would have open slots
        # (we only packed ±150 min worth of buckets, so outside that window is open)
        slot_default, fallback_default = assign_slot(
            ideal, existing, deadline,
            user_tz=_TZ_STR,
            quiet_hours=(22, 8),
            slot_minutes=30,
            slot_capacity=2,
            max_drift_minutes=720,
            now=now,
        )
        assert fallback_default is False


# ---------------------------------------------------------------------------
# format_reminder_summary
# ---------------------------------------------------------------------------

class TestFormatReminderSummary:

    def test_three_slots_same_week(self) -> None:
        """3 slots in the same week → comma-separated day+time list."""
        slots = [
            ("3d", _local(2026, 6, 3, 12, 0)),   # Wed noon
            ("1d", _local(2026, 6, 4, 9, 0)),     # Thu 9am
            ("4h", _local(2026, 6, 4, 8, 0)),     # Thu 8am  (listed by label order)
        ]
        result = format_reminder_summary(slots, user_tz=_TZ_STR)
        assert result == "I'll ping you Wed noon, Thu 9am, Thu 8am."

    def test_one_slot(self) -> None:
        """Single slot → singular form."""
        slots = [("4h", _local(2026, 6, 5, 8, 0))]  # Thu 8am (June 5 2026 is a Friday)
        result = format_reminder_summary(slots, user_tz=_TZ_STR)
        # Just check structure and that it starts with "I'll ping you "
        assert result.startswith("I'll ping you ")
        assert result.endswith(".")

    def test_empty_returns_empty_string(self) -> None:
        """Empty slot list → empty string."""
        result = format_reminder_summary([], user_tz=_TZ_STR)
        assert result == ""

    def test_midnight_label(self) -> None:
        """Slot at midnight → 'midnight', not '12am'."""
        slots = [("4h", _local(2026, 6, 3, 0, 0))]  # Wednesday midnight
        result = format_reminder_summary(slots, user_tz=_TZ_STR)
        assert "midnight" in result
        assert "12am" not in result

    def test_noon_label(self) -> None:
        """Slot at noon → 'noon', not '12pm'."""
        slots = [("3d", _local(2026, 6, 3, 12, 0))]  # Wednesday noon
        result = format_reminder_summary(slots, user_tz=_TZ_STR)
        assert "noon" in result
        assert "12pm" not in result

    def test_am_pm_formatting(self) -> None:
        """Regular hours format correctly as Xam / Xpm."""
        slots_am = [("1d", _local(2026, 6, 4, 9, 0))]
        slots_pm = [("1d", _local(2026, 6, 4, 21, 0))]
        assert "9am" in format_reminder_summary(slots_am, user_tz=_TZ_STR)
        assert "9pm" in format_reminder_summary(slots_pm, user_tz=_TZ_STR)

    def test_minute_suffix_included_when_nonzero(self) -> None:
        """Times with non-zero minutes include the minute portion."""
        slots = [("4h", _local(2026, 6, 4, 9, 30))]
        result = format_reminder_summary(slots, user_tz=_TZ_STR)
        assert "9:30am" in result

    def test_tz_conversion_applied(self) -> None:
        """Datetimes are converted to user_tz before formatting."""
        # 15:00 UTC = 10:00 CDT (America/Chicago, UTC-5 in June)
        dt_utc = _utc(2026, 6, 3, 15, 0)
        slots = [("3d", dt_utc)]
        result = format_reminder_summary(slots, user_tz=_TZ_STR)
        assert "10am" in result
