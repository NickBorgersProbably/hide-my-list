"""Structural anchors in the rendered chat prompt.

`chat.md.j2` is the only place the "what task?" recall rule and the
acceptance rule live; a prompt edit that silently drops the
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
    """The recall rule must put the newest titled Recent Tasks entry first and
    Current task as fallback — word for word, since that phrase is the contract
    the model is graded against.
    """
    rendered = _render_chat_prompt()
    assert "Newest titled Recent Tasks entry first" in rendered
    assert "Current task as fallback" in rendered
    flattened = " ".join(rendered.split())
    assert "name its title word for word" in flattened


def test_recall_precedence_active_and_recent_both_present() -> None:
    """When both an active task and a titled recent entry exist, the rendered
    prompt names the Recent Tasks ordering rule first (not Current task first).
    """
    rendered = _render_chat_prompt(
        recent_tasks='- "Water the plants" — suggested just now',
        active_task_title="Book the eye appointment",
    )
    assert "Newest titled Recent Tasks entry first" in rendered
    assert "Current task as fallback" in rendered
    assert '"Water the plants"' in rendered
    assert "Book the eye appointment" in rendered


def test_acceptance_rule_names_the_current_task() -> None:
    """An acceptance ("sure", "ok, that one") names the Current task: selection
    and rejection both activate the task they offer, so the graph already holds
    it. The newest `suggested` ledger line is only the last resort when Current
    task is None.
    """
    flattened = " ".join(_render_chat_prompt().split())
    assert "the task they accepted is the Current task above" in flattened
    assert "Only as a last resort, when Current task is \"None\"" in flattened
    assert "newest line under Recent Tasks marked `suggested`" in flattened


def test_rendered_text_contains_passed_in_values() -> None:
    rendered = _render_chat_prompt(
        recent_tasks='- "Water the plants" [reminder] — reminded just now',
        active_task_title="Book the eye appointment",
    )
    assert '"Water the plants" [reminder] — reminded just now' in rendered
    assert "Book the eye appointment" in rendered
