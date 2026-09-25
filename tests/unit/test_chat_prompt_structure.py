"""Structural anchors in the rendered chat prompt.

`chat.md.j2` is the only place the "what task?" recall rule and the
`suggested`-line acceptance rule live; a prompt edit that silently drops the
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
    """The recall rule must say Current task first, then the first Recent
    Tasks line — word for word, since that phrase is the contract the model
    is graded against.
    """
    rendered = _render_chat_prompt()
    assert "Current task first" in rendered
    flattened = " ".join(rendered.split())
    assert (
        "name the title of the first line under Recent Tasks, word for word"
        in flattened
    )


def test_acceptance_rule_mentions_newest_suggested_line() -> None:
    """When Current task is None, an acceptance ("sure", "ok, that one") must
    resolve to the newest `suggested` ledger line, not silently fall through.
    """
    rendered = _render_chat_prompt()
    assert "newest line under Recent Tasks marked" in rendered
    assert "`suggested`" in rendered


def test_rendered_text_contains_passed_in_values() -> None:
    rendered = _render_chat_prompt(
        recent_tasks='- "Water the plants" [reminder] — reminded just now',
        active_task_title="Book the eye appointment",
    )
    assert '"Water the plants" [reminder] — reminded just now' in rendered
    assert "Book the eye appointment" in rendered
