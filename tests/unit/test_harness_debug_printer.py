"""Unit coverage for the E2E harness's failing-turn debug printer.

`tests/support/harness.py::_turn` prints a failing turn's captured structlog
events when `E2E_DEBUG_TURNS` is set, so a failing CI job shows which intent
and node path the turn took. The property that matters most here is negative:
the printer must never leak message text, titles, or peers — only booleans,
counts, ids, and enum-shaped values. This file pins that filtering, not the
printing itself (which needs a live TurnResult and is exercised by the E2E
layer's own failures).
"""
from __future__ import annotations

from tests.support.harness import _safe_event_fields


def test_drops_always_private_keys_regardless_of_shape() -> None:
    entry = {
        "event": "complete_node.done",
        "peer": "+15550000001",
        "body": "done",
        "text": "done",
        "message": "hello there",
        "incoming": "done",
        "title": "Buy groceries",
        "notion_page_title": "Buy groceries",
        "content": "some free text",
        "prompt": "system prompt text",
        "reply": "you're all set",
    }
    assert _safe_event_fields(entry) == {}


def test_keeps_booleans_counts_and_short_ids() -> None:
    entry = {
        "event": "llm.call.end",
        "success": True,
        "retried": False,
        "total_tokens": 512,
        "message_count": 2,
        "page_id": "abc123def456",
        "intent": "COMPLETE",
        "tier": "cheap",
    }
    safe = _safe_event_fields(entry)
    assert safe == {
        "success": True,
        "retried": False,
        "total_tokens": 512,
        "message_count": 2,
        "page_id": "abc123def456",
        "intent": "COMPLETE",
        "tier": "cheap",
    }


def test_drops_free_text_looking_strings() -> None:
    """A string with whitespace or over-length is treated as prose, not an id."""
    entry = {
        "event": "intake_node.error",
        "error_type": "ValueError",
        "reason": "the model returned a task the peer was never offered and this runs long",
    }
    safe = _safe_event_fields(entry)
    assert safe == {"error_type": "ValueError"}
    assert "reason" not in safe


def test_drops_event_and_timestamp_keys() -> None:
    """`event` and structlog's `timestamp` are handled separately by the caller."""
    entry = {"event": "signal_listener.message_received", "timestamp": "2026-09-26T00:00:00Z"}
    assert _safe_event_fields(entry) == {}


def test_drops_nested_structures() -> None:
    entry = {"event": "x", "payload": {"a": 1}, "items": [1, 2, 3]}
    assert _safe_event_fields(entry) == {}
