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


def test_recall_rule_has_no_event_markers() -> None:
    """The rule keys on the newest rendered line, not on event words or
    markers the renderer may not emit, and makes no claim about acceptance.
    """
    flattened = " ".join(_render_chat_prompt().split())
    assert "[nudged]" not in flattened
    assert "that reminder" not in flattened
    assert "accepts a suggestion" not in flattened
    assert "marked `suggested`" not in flattened


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


def test_no_record_section_forbids_denying_unseen_history() -> None:
    """An empty Recent Tasks is a 7-day window's silence, not proof of absence.

    Without this section the model read "None yet." as "never happened" and
    told the user it had not nudged them about any deadline, which was false.
    """
    rendered = _render_chat_prompt()
    assert "### No record is not never" in rendered
    section = rendered.split("### No record is not never", 1)[1].split("\n### ", 1)[0]
    assert "no record of a recent one" in section
    assert "Never say it did not happen" in section
    assert "forward step" in section


def test_whats_left_names_no_tasks() -> None:
    """ "whats left?" is CHAT; Recent Tasks is not the list and is never enumerated."""
    rendered = _render_chat_prompt()
    assert "### What's on my list?" in rendered
    section = " ".join(
        rendered.split("### What's on my list?", 1)[1].split("\n### ", 1)[0].split()
    )
    assert "do not name, list, or count the tasks" in section
    assert "do not say the list is empty" in section
    assert "Recent Tasks is not the list" in section


def test_guidelines_never_offer_the_task_list() -> None:
    """No module can honor "want to see your other tasks?"; the step is one suggestion."""
    rendered = _render_chat_prompt()
    guidelines = rendered.split("### Response Guidelines", 1)[1].split("\n### ", 1)[0]
    assert "Never offer to show, list, or enumerate the user's tasks" in guidelines
    assert "want a suggestion?" in guidelines
    assert "adding something new" in guidelines
