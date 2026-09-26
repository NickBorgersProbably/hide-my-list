"""Structural anchors in the rendered chat prompt.

`chat.md.j2` is the only place the "what task?" recall rule lives; a prompt
edit that silently drops the ordering instruction or the anchor headings breaks recall without breaking any
LLM-graded eval (evals are gated and rarely run locally). This is a string-
presence check against the rendered template, not an LLM test: it can run in
`tests/unit` with no proxy and no DATABASE_URL.
"""
from __future__ import annotations

from app.prompts.loader import render_with_defaults


def _render_chat_prompt(**overrides: object) -> str:
    """Render chat.md.j2 with a minimal context, mirroring
    tests/unit/test_prompt_parity.py's `_render_template_with_empty_context`.
    """
    context: dict[str, object] = {
        "user_message": "",
        "conversation_context": "No prior context.",
        "recent_tasks": "None yet.",
        "active_task_title": "None",
    }
    context.update(overrides)
    return render_with_defaults("chat.md.j2", context)


def test_section_anchors_present() -> None:
    rendered = _render_chat_prompt()
    assert "### Recent Tasks" in rendered
    assert "### Which task?" in rendered


def test_recall_instruction_names_the_ordering() -> None:
    """The recall rule names the newest Recent Tasks entry word for word, asks
    the user to name the task when that entry is untitled (never skipping to
    an older entry), and uses the Current task only when Recent Tasks is empty
    — in that order, since that ordering is the contract the model is graded
    against.
    """
    flattened = " ".join(_render_chat_prompt().split())
    newest_rule = "Find the newest entry under Recent Tasks that is not `rejected`"
    untitled_rule = "Name that entry's title word for word. If it is `(untitled)`, say you are not sure which one"
    fallback_rule = "If every entry is `rejected`, or Recent Tasks is \"None yet.\", name the Current task word for word"
    for phrase in (newest_rule, untitled_rule, fallback_rule):
        assert phrase in flattened
    assert (
        flattened.index(newest_rule)
        < flattened.index(untitled_rule)
        < flattened.index(fallback_rule)
    )
    assert "A `rejected` entry was declined by the user" in flattened


def _recall_section() -> str:
    """The "### Which task?" section alone, flattened to single spaces."""
    rendered = _render_chat_prompt()
    start = rendered.index("### Which task?")
    end = rendered.index("\n### ", start + 1)
    return " ".join(rendered[start:end].split())


def test_recall_rule_has_no_event_markers() -> None:
    """The rule keys on the newest rendered line, not on event words or
    markers the renderer may not emit, and makes no claim about acceptance.

    Scoped to the recall section: acceptance has its own section
    ("### Accepting a suggestion"), and the recall rule must not borrow it.
    """
    recall = _recall_section()
    assert "[nudged]" not in recall
    assert "that reminder" not in recall
    assert "accepts a suggestion" not in recall
    assert "marked `suggested`" not in recall
    # The prompt as a whole never resolves acceptance through a `suggested`
    # ledger line; acceptance names the Current task only.
    assert "marked `suggested`" not in " ".join(_render_chat_prompt().split())


def test_recall_precedence_active_and_recent_both_present() -> None:
    """When both an active task and a recent entry exist, the rendered prompt
    names the Recent Tasks rule before the Current task fallback.
    """
    rendered = _render_chat_prompt(
        recent_tasks='- "Water the plants" — suggested just now',
        active_task_title="Book the eye appointment",
    )
    flattened = " ".join(rendered.split())
    assert flattened.index("newest entry under Recent Tasks") < flattened.index(
        "name the Current task"
    )
    assert '"Water the plants"' in rendered
    assert "Book the eye appointment" in rendered


def test_rendered_text_contains_passed_in_values() -> None:
    rendered = _render_chat_prompt(
        recent_tasks='- "Water the plants" [reminder] — reminded just now',
        active_task_title="Book the eye appointment",
    )
    assert '"Water the plants" [reminder] — reminded just now' in rendered
    assert "Book the eye appointment" in rendered
