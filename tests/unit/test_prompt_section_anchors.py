"""Structural anchor guards for prompts that gained new sections in bug_0677.

Checks that intake.md.j2 contains the two new follow-up anchors and that the
three context-aware prompts (cannot_finish, need_help, rejection) contain the
Prior Conversation and Recent Tasks section headers.

These are raw-text presence checks, not rendered-output checks — the anchors
are unconditional in all four templates, so rendering is not required.
"""
from __future__ import annotations

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "app" / "prompts"


def _raw(filename: str) -> str:
    path = _PROMPTS_DIR / filename
    assert path.is_file(), f"Template not found: {path}"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# intake.md.j2 — new follow-up and already-done anchors
# ---------------------------------------------------------------------------


def test_intake_follow_ups_anchor_present() -> None:
    assert "### Follow-Ups That Name An Earlier Message" in _raw("intake.md.j2"), (
        "intake.md.j2 must contain '### Follow-Ups That Name An Earlier Message'"
    )


def test_intake_already_done_anchor_present() -> None:
    assert "### Already Done Reports" in _raw("intake.md.j2"), (
        "intake.md.j2 must contain '### Already Done Reports'"
    )


# ---------------------------------------------------------------------------
# cannot_finish.md.j2 — context section anchors
# ---------------------------------------------------------------------------


def test_cannot_finish_prior_conversation_anchor_present() -> None:
    assert "### Prior Conversation" in _raw("cannot_finish.md.j2"), (
        "cannot_finish.md.j2 must contain '### Prior Conversation'"
    )


def test_cannot_finish_recent_tasks_anchor_present() -> None:
    assert "### Recent Tasks" in _raw("cannot_finish.md.j2"), (
        "cannot_finish.md.j2 must contain '### Recent Tasks'"
    )


# ---------------------------------------------------------------------------
# need_help.md.j2 — context section anchors
# ---------------------------------------------------------------------------


def test_need_help_prior_conversation_anchor_present() -> None:
    assert "### Prior Conversation" in _raw("need_help.md.j2"), (
        "need_help.md.j2 must contain '### Prior Conversation'"
    )


def test_need_help_recent_tasks_anchor_present() -> None:
    assert "### Recent Tasks" in _raw("need_help.md.j2"), (
        "need_help.md.j2 must contain '### Recent Tasks'"
    )


# ---------------------------------------------------------------------------
# rejection.md.j2 — context section anchors
# ---------------------------------------------------------------------------


def test_rejection_prior_conversation_anchor_present() -> None:
    assert "### Prior Conversation" in _raw("rejection.md.j2"), (
        "rejection.md.j2 must contain '### Prior Conversation'"
    )


def test_rejection_recent_tasks_anchor_present() -> None:
    assert "### Recent Tasks" in _raw("rejection.md.j2"), (
        "rejection.md.j2 must contain '### Recent Tasks'"
    )
