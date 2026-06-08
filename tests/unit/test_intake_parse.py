"""Unit tests for intake LLM response parsing."""
from __future__ import annotations

from app.graph.nodes.intake import _parse_intake_response


def test_parse_intake_response_accepts_json_object() -> None:
    parsed = _parse_intake_response(
        '{"action": "save", "title": "Test task", "is_reminder": false}'
    )

    assert parsed is not None
    assert parsed["action"] == "save"
    assert parsed["title"] == "Test task"


def test_parse_intake_response_returns_none_for_non_json() -> None:
    assert _parse_intake_response("I can help with that.") is None


def test_parse_intake_response_returns_none_for_truncated_json() -> None:
    assert _parse_intake_response(
        '{"action": "save", "title": "Test task", "is_reminder": true,'
    ) is None


def test_parse_intake_response_returns_none_for_non_object_json() -> None:
    assert _parse_intake_response('["not", "an", "object"]') is None
