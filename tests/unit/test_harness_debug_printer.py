"""Unit coverage for the E2E harness's failing-turn debug printer.

`tests/support/harness.py::_turn` prints a failing turn's captured structlog
events when `E2E_DEBUG_TURNS` is set, so a failing CI job shows which intent
and node path the turn took. The property that matters most here is negative:
the printer must never leak message text, titles, or peers — only booleans,
counts, and strings under an explicit key allowlist. This file pins that
filtering, and pins `_check_turn`'s wiring: the dump runs only when
`E2E_DEBUG_TURNS` is enabled, and the original assertion always propagates.
"""
from __future__ import annotations

from typing import Any

import pytest

import tests.support.harness as harness
from tests.support.harness import Expect, TurnResult, _check_turn, _safe_event_fields


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


def test_drops_free_text_under_allowlisted_keys() -> None:
    """An allowlisted key still drops a value with whitespace or over the length cap."""
    entry = {
        "event": "intake_node.error",
        "error_type": "ValueError",
        "reason": "the model returned a task the peer was never offered",
        "kind": "x" * 65,
    }
    safe = _safe_event_fields(entry)
    assert safe == {"error_type": "ValueError"}


@pytest.mark.parametrize("value", ["+15550000001", "<recipient>", "Groceries", "abc123"])
def test_drops_short_token_strings_under_unknown_keys(value: str) -> None:
    """A short, whitespace-free string under a key off the allowlist is dropped.

    Shape says nothing about privacy: a phone number or a one-word task title
    looks exactly like an id. Only the key allowlist decides.
    """
    entry = {"event": "x", "unlisted_field": value, "has_peer": True}
    assert _safe_event_fields(entry) == {"has_peer": True}


def test_drops_event_and_timestamp_keys() -> None:
    """`event` and structlog's `timestamp` are handled separately by the caller."""
    entry = {"event": "signal_listener.message_received", "timestamp": "2026-09-26T00:00:00Z"}
    assert _safe_event_fields(entry) == {}


def test_drops_nested_structures() -> None:
    entry = {"event": "x", "payload": {"a": 1}, "items": [1, 2, 3]}
    assert _safe_event_fields(entry) == {}


def test_exception_class_processor_names_the_class_only() -> None:
    """Inside an `except`, a `log.exception` entry gains the class name, not the message."""
    import structlog

    from tests.support.harness import _CAPTURE_PROCESSORS

    log = structlog.get_logger("test")
    with structlog.testing.capture_logs(processors=_CAPTURE_PROCESSORS) as logs:
        try:
            raise ValueError("private text that must not be captured")
        except ValueError:
            log.exception("intake_node.error", has_peer=True)
        log.info("intake_node.parsed", is_reminder=True)

    error_entry = next(e for e in logs if e["event"] == "intake_node.error")
    assert error_entry["exception_class"] == "ValueError"
    assert "private text" not in repr(error_entry)
    assert _safe_event_fields(error_entry) == {
        "has_peer": True,
        "exc_info": True,
        "exception_class": "ValueError",
        "log_level": "error",
    }
    info_entry = next(e for e in logs if e["event"] == "intake_node.parsed")
    assert "exception_class" not in info_entry


def _turn_result() -> TurnResult:
    return TurnResult(
        text="Test message",
        sent=[],
        state={},
        logs=[
            {"event": "router.classified", "intent": "COMPLETE", "peer": "<recipient>"},
            {"event": "send_node.sent", "unlisted_field": "Test message"},
        ],
        notion_writes_since=0,
        awaiting_reply_before=0,
        awaiting_reply_after=0,
        graph_invoked=True,
    )


def _raise_assertion(*_args: Any) -> None:
    raise AssertionError("invariant broke")


def test_check_turn_prints_debug_and_reraises_when_enabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("E2E_DEBUG_TURNS", "true")
    monkeypatch.setattr(harness, "assert_turn_invariants", _raise_assertion)

    with pytest.raises(AssertionError, match="invariant broke"):
        _check_turn(None, _turn_result(), Expect())  # type: ignore[arg-type]

    out = capsys.readouterr().out
    assert "[e2e-debug] turn events:" in out
    assert "router.classified" in out
    assert "'intent': 'COMPLETE'" in out
    assert "delivered reply length: 12 chars" in out
    assert "<recipient>" not in out
    assert "Test message" not in out


def test_check_turn_prints_debug_on_expectation_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("E2E_DEBUG_TURNS", "1")
    monkeypatch.setattr(harness, "assert_turn_invariants", lambda *_args: None)
    monkeypatch.setattr(harness, "assert_expectations", _raise_assertion)

    with pytest.raises(AssertionError, match="invariant broke"):
        _check_turn(None, _turn_result(), Expect())  # type: ignore[arg-type]

    assert "[e2e-debug] turn events:" in capsys.readouterr().out


@pytest.mark.parametrize("setting", [None, "", "false", "0"])
def test_check_turn_is_silent_and_reraises_when_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], setting: str | None
) -> None:
    if setting is None:
        monkeypatch.delenv("E2E_DEBUG_TURNS", raising=False)
    else:
        monkeypatch.setenv("E2E_DEBUG_TURNS", setting)
    monkeypatch.setattr(harness, "assert_turn_invariants", _raise_assertion)

    with pytest.raises(AssertionError, match="invariant broke"):
        _check_turn(None, _turn_result(), Expect())  # type: ignore[arg-type]

    assert capsys.readouterr().out == ""


def test_check_turn_is_silent_when_assertions_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("E2E_DEBUG_TURNS", "true")
    monkeypatch.setattr(harness, "assert_turn_invariants", lambda *_args: None)
    monkeypatch.setattr(harness, "assert_expectations", lambda *_args: None)

    _check_turn(None, _turn_result(), Expect())  # type: ignore[arg-type]

    assert capsys.readouterr().out == ""
