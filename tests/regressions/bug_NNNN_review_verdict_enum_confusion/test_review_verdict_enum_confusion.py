"""Regression: an action name in the review's `verdict` field (bug #NNNN).

The model answered the one shape the review exists for with
`{"verdict": "complete_task", "action": "complete_task", ...}`, and the parser
threw the correct repair away as `unknown_verdict`. The shape is now read as
`correct`; contradictions stay rejected so the normalization cannot widen what
the review is allowed to do.
"""
from __future__ import annotations

import json

import pytest
from structlog.testing import capture_logs

from app.graph.interaction_review import parse_verdict

_OPEN = {"<page_reminder>"}


def _text(verdict: str, action: str, page_id: str | None = "<page_reminder>") -> str:
    return json.dumps({
        "verdict": verdict,
        "reason": "placeholder reason",
        "action": action,
        "page_id": page_id,
    })


def test_the_observed_shape_is_read_as_a_correction() -> None:
    with capture_logs() as logs:
        verdict = parse_verdict(
            _text("complete_task", "complete_task"),
            open_page_ids=_OPEN,
            completed_this_turn=set(),
        )
    assert verdict is not None
    assert (verdict.verdict, verdict.action, verdict.page_id) == (
        "correct", "complete_task", "<page_reminder>",
    )
    assert [e["event"] for e in logs] == ["interaction_review.verdict_normalized"]


@pytest.mark.parametrize(
    ("verdict", "action", "page_id"),
    [
        ("send_only", "complete_task", "<page_reminder>"),
        ("none", "complete_task", "<page_reminder>"),
        ("complete_task", "complete_task", "<page_invented>"),
    ],
)
def test_contradictions_and_bad_pages_stay_rejected(
    verdict: str, action: str, page_id: str
) -> None:
    with capture_logs() as logs:
        assert parse_verdict(
            _text(verdict, action, page_id),
            open_page_ids=_OPEN,
            completed_this_turn=set(),
        ) is None
    assert [e["event"] for e in logs][-1] == "interaction_review.verdict_rejected"
