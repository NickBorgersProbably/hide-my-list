"""Structural anchors in the rendered chat prompt.

`chat.md.j2` is the only place the "what task?" recall rule and the model's
acceptance rule live (accepting a pending suggestion with a bare "sure" is
handled in `chat_node` code before the model runs); a prompt edit that silently drops the
ordering instruction or the anchor headings breaks recall without breaking any
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
    """The recall rule answers about a newest reminder first (even an untitled
    one, without skipping to an older task), then the newest titled Recent
    Tasks entry, then the Current task as fallback — in that order, since that
    ordering is the contract the model is graded against.
    """
    flattened = " ".join(_render_chat_prompt().split())
    reminder_rule = "If the newest entry in Recent Tasks has event `reminded` or `nudged`"
    titled_rule = "Otherwise, if Recent Tasks has a titled entry"
    fallback_rule = "Current task as fallback"
    for phrase in (reminder_rule, titled_rule, fallback_rule):
        assert phrase in flattened
    assert (
        flattened.index(reminder_rule)
        < flattened.index(titled_rule)
        < flattened.index(fallback_rule)
    )
    assert "do not skip to an older titled task entry" in flattened
    assert "name its title word for word" in flattened


def test_recall_precedence_active_and_recent_both_present() -> None:
    """When both an active task and a titled recent entry exist, the rendered
    prompt names the Recent Tasks rule before the Current task fallback.
    """
    rendered = _render_chat_prompt(
        recent_tasks='- "Water the plants" — suggested just now',
        active_task_title="Book the eye appointment",
    )
    flattened = " ".join(rendered.split())
    assert flattened.index("Otherwise, if Recent Tasks has a titled entry") < flattened.index(
        "Current task as fallback"
    )
    assert '"Water the plants"' in rendered
    assert "Book the eye appointment" in rendered


def test_acceptance_rule_names_the_current_task() -> None:
    """With acceptance of a pending suggestion handled in code (chat_node's
    deterministic path), the prompt only needs to name the Current task when
    the user accepts and one is set. The `suggested`-line fallback is gone:
    the model must never guess a task the graph does not hold.
    """
    flattened = " ".join(_render_chat_prompt().split())
    assert (
        "When the user accepts a suggestion and Current task is set, name it"
        in flattened
    )
    assert "marked `suggested`" not in flattened
    assert "last resort" not in flattened


def test_rendered_text_contains_passed_in_values() -> None:
    rendered = _render_chat_prompt(
        recent_tasks='- "Water the plants" [reminder] — reminded just now',
        active_task_title="Book the eye appointment",
    )
    assert '"Water the plants" [reminder] — reminded just now' in rendered
    assert "Book the eye appointment" in rendered
