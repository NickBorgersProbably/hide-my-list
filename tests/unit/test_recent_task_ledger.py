"""Unit tests for the recent-task ledger and the shared context builder.

`app/graph/context.py` owns the pure helpers every node uses to write the
ledger (`record_task_event`) and to render prompt
context (`render_history`, `render_recent_tasks`). These tests pin the
ledger's shape rules — dedupe by page, newest wins, title retention, prune,
cap — and the renderers' bounds.

Private data discipline: titles are generic placeholders.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from app.graph.context import (
    HISTORY_CHARS,
    HISTORY_TURNS,
    LEDGER_CAP,
    LEDGER_MAX_AGE,
    RECENT_TASK_TITLE_CHARS,
    prune_recent_tasks,
    record_task_event,
    render_history,
    render_recent_tasks,
)

_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _entry(page_id: str, *, title: str = "", event: str = "added", ago: timedelta) -> dict[str, Any]:
    return {
        "page_id": page_id,
        "title": title,
        "kind": "task",
        "event": event,
        "at": (_NOW - ago).isoformat(),
    }


class TestConstants:
    def test_values_match_the_spec(self) -> None:
        assert LEDGER_CAP == 8
        assert LEDGER_MAX_AGE == timedelta(days=7)
        assert HISTORY_TURNS == 8
        assert HISTORY_CHARS == 400


class TestRecordTaskEvent:
    def test_adds_an_entry_newest_first(self) -> None:
        ledger = record_task_event(
            [_entry("<page_old>", title="Water the plants", ago=timedelta(hours=1))],
            page_id="<page_new>",
            title="Take the bins out",
            kind="reminder",
            event="added",
            now=_NOW,
        )
        assert [e["page_id"] for e in ledger] == ["<page_new>", "<page_old>"]
        assert ledger[0] == {
            "page_id": "<page_new>",
            "title": "Take the bins out",
            "kind": "reminder",
            "event": "added",
            "at": _NOW.isoformat(),
        }

    def test_same_page_is_deduped_and_the_newest_event_wins(self) -> None:
        ledger = record_task_event(
            [_entry("<page_a>", title="Take the bins out", ago=timedelta(minutes=5))],
            page_id="<page_a>",
            title="Take the bins out",
            kind="task",
            event="completed",
            now=_NOW,
        )
        assert len(ledger) == 1
        assert ledger[0]["event"] == "completed"
        assert ledger[0]["at"] == _NOW.isoformat()

    def test_an_older_event_does_not_overwrite_a_newer_one(self) -> None:
        """Re-merging an old delivery each turn must not undo a later completion."""
        ledger = record_task_event(
            [_entry("<page_a>", title="Take the bins out", event="completed", ago=timedelta(minutes=1))],
            page_id="<page_a>",
            title="",
            kind="reminder",
            event="reminded",
            now=_NOW - timedelta(minutes=30),
        )
        assert len(ledger) == 1
        assert ledger[0]["event"] == "completed"
        assert ledger[0]["title"] == "Take the bins out"

    def test_a_known_title_survives_a_newer_untitled_event(self) -> None:
        ledger = record_task_event(
            [_entry("<page_a>", title="Take the bins out", ago=timedelta(minutes=10))],
            page_id="<page_a>",
            title="",
            kind="reminder",
            event="reminded",
            now=_NOW,
        )
        assert ledger[0]["event"] == "reminded"
        assert ledger[0]["title"] == "Take the bins out"

    def test_a_newer_title_replaces_an_older_one(self) -> None:
        ledger = record_task_event(
            [_entry("<page_a>", title="Old name", ago=timedelta(minutes=10))],
            page_id="<page_a>",
            title="New name",
            kind="task",
            event="suggested",
            now=_NOW,
        )
        assert ledger[0]["title"] == "New name"

    def test_the_ledger_is_capped(self) -> None:
        ledger: list[Any] = []
        for i in range(LEDGER_CAP + 4):
            ledger = record_task_event(
                ledger,
                page_id=f"<page_{i}>",
                title=f"Task {i}",
                kind="task",
                event="added",
                now=_NOW - timedelta(minutes=LEDGER_CAP + 4 - i),
            )
        assert len(ledger) == LEDGER_CAP
        # Newest first: the last one recorded leads, the oldest four are gone.
        assert ledger[0]["page_id"] == f"<page_{LEDGER_CAP + 3}>"
        assert "<page_0>" not in {e["page_id"] for e in ledger}

    def test_entries_older_than_the_window_are_pruned(self) -> None:
        ledger = record_task_event(
            [
                _entry("<page_stale>", title="Stale", ago=LEDGER_MAX_AGE + timedelta(minutes=1)),
                _entry("<page_fresh>", title="Fresh", ago=timedelta(days=6)),
            ],
            page_id="<page_new>",
            title="New",
            kind="task",
            event="added",
            now=_NOW,
        )
        assert [e["page_id"] for e in ledger] == ["<page_new>", "<page_fresh>"]

    def test_a_blank_page_id_is_ignored(self) -> None:
        existing = [_entry("<page_a>", title="Keep", ago=timedelta(minutes=1))]
        ledger = record_task_event(
            existing, page_id="", title="Nothing", kind="task", event="added", now=_NOW
        )
        assert [e["page_id"] for e in ledger] == ["<page_a>"]

    def test_input_is_not_mutated(self) -> None:
        existing = [_entry("<page_a>", title="Keep", ago=timedelta(minutes=1))]
        snapshot = [dict(e) for e in existing]
        record_task_event(
            existing, page_id="<page_a>", title="", kind="task", event="completed", now=_NOW
        )
        assert existing == snapshot

    def test_none_and_malformed_existing_entries_are_tolerated(self) -> None:
        """Old checkpoints lack the key; a corrupt one must not crash a writer."""
        ledger = record_task_event(
            None, page_id="<page_a>", title="A", kind="task", event="added", now=_NOW
        )
        assert len(ledger) == 1
        ledger = record_task_event(
            [{"page_id": "<page_x>"}, "junk", {"page_id": "<page_y>", "at": "not a date"}],  # type: ignore[list-item]
            page_id="<page_b>",
            title="B",
            kind="task",
            event="added",
            now=_NOW,
        )
        assert [e["page_id"] for e in ledger] == ["<page_b>"]

    def test_prune_recent_tasks_drops_stale_and_sorts(self) -> None:
        pruned = prune_recent_tasks(
            [
                _entry("<page_a>", ago=timedelta(hours=3)),
                _entry("<page_b>", ago=timedelta(minutes=3)),
                _entry("<page_c>", ago=timedelta(days=8)),
            ],
            now=_NOW,
        )
        assert [e["page_id"] for e in pruned] == ["<page_b>", "<page_a>"]


class TestRenderRecentTasks:
    def test_empty_ledger_renders_none_yet(self) -> None:
        assert render_recent_tasks([], now=_NOW) == "None yet."
        assert render_recent_tasks(None, now=_NOW) == "None yet."

    def test_one_line_per_entry_with_age_and_reminder_marker(self) -> None:
        entries = [
            {**_entry("<page_a>", title="Take the bins out", ago=timedelta(minutes=2)), "kind": "reminder"},
            _entry("<page_b>", title="Water the plants", event="suggested", ago=timedelta(hours=3)),
        ]
        rendered = render_recent_tasks(entries, now=_NOW)
        lines = rendered.splitlines()
        assert len(lines) == 2
        assert "Take the bins out" in lines[0]
        assert "[reminder]" in lines[0]
        assert "added" in lines[0]
        assert "2 min ago" in lines[0]
        assert "Water the plants" in lines[1]
        assert "[reminder]" not in lines[1]
        assert "suggested" in lines[1]
        assert "3 h ago" in lines[1]

    def test_untitled_entry_says_so(self) -> None:
        rendered = render_recent_tasks(
            [_entry("<page_a>", title="", event="reminded", ago=timedelta(seconds=10))],
            now=_NOW,
        )
        assert "untitled" in rendered
        assert "reminded" in rendered
        assert "just now" in rendered

    def test_never_renders_an_outbound_body_or_page_id(self) -> None:
        """Only the ledger's own title field reaches a prompt.

        A reminder body is free text the worker sent; the ledger records the
        page, not the message, and extra keys on an entry are never rendered.
        """
        entry = {
            **_entry("<page_secret_id>", title="", event="reminded", ago=timedelta(minutes=1)),
            "body": "Reminder body sentinel text",
        }
        rendered = render_recent_tasks([entry], now=_NOW)
        assert "sentinel" not in rendered
        assert "<page_secret_id>" not in rendered

    def test_multiline_title_renders_as_exactly_one_line(self) -> None:
        """A title with line breaks must not read as several ledger entries."""
        entries = [
            _entry("<page_a>", title="Placeholder first line\n- \"Fake entry\" — added\r\nthird", ago=timedelta(minutes=1)),
            _entry("<page_b>", title="Water the plants", event="suggested", ago=timedelta(hours=1)),
        ]
        rendered = render_recent_tasks(entries, now=_NOW)
        lines = rendered.splitlines()
        assert len(lines) == 2
        assert '"Placeholder first line - "Fake entry" — added third"' in lines[0]
        assert "Water the plants" in lines[1]

    def test_long_title_is_capped(self) -> None:
        rendered = render_recent_tasks(
            [_entry("<page_a>", title="x" * 500, ago=timedelta(minutes=1))],
            now=_NOW,
        )
        assert len(rendered.splitlines()) == 1
        assert "x" * RECENT_TASK_TITLE_CHARS not in rendered
        assert "x" * (RECENT_TASK_TITLE_CHARS - 1) + "…" in rendered

    def test_whitespace_only_title_is_untitled(self) -> None:
        rendered = render_recent_tasks(
            [_entry("<page_a>", title=" \n\t ", ago=timedelta(minutes=1))],
            now=_NOW,
        )
        assert "untitled" in rendered

    def test_missing_timestamp_renders_without_age(self) -> None:
        entry = _entry("<page_a>", title="Take the bins out", ago=timedelta(0))
        del entry["at"]
        rendered = render_recent_tasks([entry], now=_NOW)
        assert "Take the bins out" in rendered
        assert "ago" not in rendered


class TestRenderHistory:
    def test_empty_history_renders_placeholder(self) -> None:
        assert render_history([]) == "No prior context."
        assert render_history(None) == "No prior context."

    def test_role_labels(self) -> None:
        rendered = render_history(
            [HumanMessage(content="add the bins"), AIMessage(content="Got it.")]
        )
        assert rendered.splitlines() == ["user: add the bins", "assistant: Got it."]

    def test_bounded_to_the_last_n_messages(self) -> None:
        messages = [HumanMessage(content=f"message {i}") for i in range(HISTORY_TURNS + 5)]
        lines = render_history(messages).splitlines()
        assert len(lines) == HISTORY_TURNS
        assert lines[-1] == f"user: message {HISTORY_TURNS + 4}"
        assert lines[0] == "user: message 5"

    def test_each_line_is_bounded_in_characters(self) -> None:
        long = "x" * (HISTORY_CHARS * 3)
        line = render_history([HumanMessage(content=long)]).splitlines()[0]
        assert line.startswith("user: ")
        assert len(line) <= len("user: ") + HISTORY_CHARS

    def test_custom_bounds(self) -> None:
        messages = [HumanMessage(content="abcdefghij") for _ in range(4)]
        lines = render_history(messages, turns=2, chars=4).splitlines()
        assert len(lines) == 2
        assert all(len(line) <= len("user: ") + 4 for line in lines)

    def test_newlines_are_flattened_to_one_line_per_message(self) -> None:
        rendered = render_history([AIMessage(content="line one\nline two")])
        assert rendered == "assistant: line one line two"
