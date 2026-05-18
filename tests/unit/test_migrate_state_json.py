"""Unit tests for scripts/migrate_state_json.py.

Uses synthetic state.json fixtures — no real task titles, phone numbers,
reminder content, or personal data.

Tests cover:
  - _extract_user_prefs: normal, missing, wrong type
  - _extract_recent_outbound: unexpired, expired, malformed, missing fields
  - _extract_reward_prefs: valid values, invalid values fall back to defaults
  - main dry-run: loads fixture, prints plan, no DB writes
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

# Ensure scripts/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

import migrate_state_json as mig  # noqa: E402  (after sys.path insert)


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# _extract_user_prefs
# ---------------------------------------------------------------------------

def test_extract_user_prefs_normal() -> None:
    state = {"user_preferences": {"beverage": "coffee", "focus_mode": True}}
    result = mig._extract_user_prefs(state)
    assert result == {"beverage": "coffee", "focus_mode": True}


def test_extract_user_prefs_missing_returns_empty() -> None:
    state: dict = {}
    result = mig._extract_user_prefs(state)
    assert result == {}


def test_extract_user_prefs_wrong_type_returns_empty(capsys: pytest.CaptureFixture[str]) -> None:
    state = {"user_preferences": "not-a-dict"}
    result = mig._extract_user_prefs(state)
    assert result == {}
    captured = capsys.readouterr()
    assert "WARNING" in captured.err


# ---------------------------------------------------------------------------
# _extract_reward_prefs
# ---------------------------------------------------------------------------

def test_extract_reward_prefs_valid() -> None:
    prefs = {
        "reward_intensity": "high",
        "reward_kinds_enabled": ["emoji"],
        "sensitive_task_mode": True,
    }
    result = mig._extract_reward_prefs(prefs)
    assert result["reward_intensity"] == "high"
    assert json.loads(result["reward_kinds_enabled"]) == ["emoji"]
    assert result["sensitive_task_mode"] is True


def test_extract_reward_prefs_invalid_intensity_defaults_to_medium() -> None:
    prefs = {"reward_intensity": "galactic"}
    result = mig._extract_reward_prefs(prefs)
    assert result["reward_intensity"] == "medium"


def test_extract_reward_prefs_missing_uses_defaults() -> None:
    result = mig._extract_reward_prefs({})
    assert result["reward_intensity"] == "medium"
    assert json.loads(result["reward_kinds_enabled"]) == ["emoji", "image"]
    assert result["sensitive_task_mode"] is False


# ---------------------------------------------------------------------------
# _extract_recent_outbound
# ---------------------------------------------------------------------------

def test_extract_recent_outbound_valid_unexpired() -> None:
    now = _now()
    future = (now + timedelta(hours=12)).isoformat()
    past = now.isoformat()
    state = {
        "recent_outbound": [
            {
                "page_id": "page-aaa",
                "signal_timestamp": 10001,
                "sent_at": past,
                "expires_at": future,
                "type": "reminder",
                "status": "sent",
                "awaiting_response": True,
            }
        ]
    }
    rows = mig._extract_recent_outbound(state, "+10000000001")
    assert len(rows) == 1
    assert rows[0]["notion_page_id"] == "page-aaa"
    assert rows[0]["peer"] == "+10000000001"
    assert rows[0]["title"] == ""  # Private data NOT migrated


def test_extract_recent_outbound_expired_skipped() -> None:
    now = _now()
    past_expires = (now - timedelta(hours=1)).isoformat()
    state = {
        "recent_outbound": [
            {
                "page_id": "page-expired",
                "sent_at": now.isoformat(),
                "expires_at": past_expires,
            }
        ]
    }
    rows = mig._extract_recent_outbound(state, "+10000000001")
    assert rows == []


def test_extract_recent_outbound_missing_page_id_skipped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = {"recent_outbound": [{"signal_timestamp": 9999}]}
    rows = mig._extract_recent_outbound(state, "+10000000001")
    assert rows == []
    captured = capsys.readouterr()
    assert "WARNING" in captured.err


def test_extract_recent_outbound_missing_list_returns_empty(
    capsys: pytest.CaptureFixture[str],
) -> None:
    state: dict = {}
    rows = mig._extract_recent_outbound(state, "+10000000001")
    assert rows == []


def test_extract_recent_outbound_wrong_type(
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = {"recent_outbound": "not-a-list"}
    rows = mig._extract_recent_outbound(state, "+10000000001")
    assert rows == []
    captured = capsys.readouterr()
    assert "WARNING" in captured.err


def test_extract_recent_outbound_multiple_entries_only_unexpired() -> None:
    now = _now()
    state = {
        "recent_outbound": [
            # Unexpired
            {
                "page_id": "page-fresh",
                "expires_at": (now + timedelta(hours=10)).isoformat(),
                "sent_at": now.isoformat(),
            },
            # Expired
            {
                "page_id": "page-stale",
                "expires_at": (now - timedelta(hours=2)).isoformat(),
                "sent_at": now.isoformat(),
            },
        ]
    }
    rows = mig._extract_recent_outbound(state, "+10000000001")
    assert len(rows) == 1
    assert rows[0]["notion_page_id"] == "page-fresh"


def test_extract_recent_outbound_synthetic_timestamp_assigned() -> None:
    """Entries without signal_timestamp get a synthetic one."""
    now = _now()
    state = {
        "recent_outbound": [
            {
                "page_id": "page-no-ts",
                "expires_at": (now + timedelta(hours=10)).isoformat(),
                "sent_at": now.isoformat(),
            }
        ]
    }
    rows = mig._extract_recent_outbound(state, "+10000000001")
    assert len(rows) == 1
    assert isinstance(rows[0]["signal_timestamp"], int)
    assert rows[0]["signal_timestamp"] > 0


# ---------------------------------------------------------------------------
# dry-run end-to-end
# ---------------------------------------------------------------------------

def test_dry_run_loads_fixture_and_prints_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """dry-run reads a synthetic state.json and prints the plan without DB writes."""
    now = _now()
    fixture: dict = {
        "user_preferences": {
            "beverage": "tea",
            "reward_intensity": "low",
        },
        "streak": 5,  # NOT migrated
        "active_task": {"title": "Placeholder task"},  # NOT migrated
        "tasks_completed_today": 3,  # NOT migrated
        "recent_outbound": [
            {
                "page_id": "page-abc",
                "expires_at": (now + timedelta(hours=6)).isoformat(),
                "sent_at": now.isoformat(),
                "type": "reminder",
                "status": "sent",
            }
        ],
    }
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(fixture), encoding="utf-8")

    monkeypatch.setattr(
        sys, "argv",
        ["migrate_state_json.py", "--state-json", str(state_file),
         "--peer", "+10000000001", "--dry-run"],
    )

    mig.main()

    out, err = capsys.readouterr()
    assert "DRY RUN" in out
    assert "user_prefs UPSERT" in out
    assert "recent_outbound" in out
    assert "streak" in out  # Discarded items listed
    assert "active_task" in out
    # Peer (phone number) must not appear in dry-run output.
    assert "+10000000001" not in out
    assert "+10000000001" not in err
    assert "Placeholder task" not in out
    assert err == ""


def test_main_exits_without_state_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """main() exits with code 1 if state.json does not exist."""
    missing = tmp_path / "no-state.json"
    monkeypatch.setattr(
        sys, "argv",
        ["migrate_state_json.py", "--state-json", str(missing),
         "--peer", "+10000000001", "--dry-run"],
    )
    with pytest.raises(SystemExit) as exc_info:
        mig.main()
    assert exc_info.value.code == 1


def test_main_exits_without_peer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """main() exits with code 1 if --peer is not provided."""
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({}), encoding="utf-8")

    monkeypatch.setattr(
        sys, "argv",
        ["migrate_state_json.py", "--state-json", str(state_file), "--dry-run"],
    )
    # Remove SIGNAL_ACCOUNT so the env fallback doesn't kick in.
    monkeypatch.delenv("SIGNAL_ACCOUNT", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        mig.main()
    assert exc_info.value.code == 1
