"""Unit tests for the COMPLETE node's task-reference gate.

_task_reference_tokens decides whether a completion message names a task. An
empty result means "resolve from context" and costs nothing; a non-empty result
buys a Notion read and possibly a model call. Both directions are pinned here
because the two failure modes are asymmetric:

- Wrongly deciding a message names something: one Notion read that shortlists
  nothing, then the same context-based resolution as before. Cheap.
- Wrongly deciding a message names nothing: the named task is never looked up,
  which is the production bug this path exists to fix.

Pure functions only — no mocks, no LLM, no Notion.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.graph.nodes._task_match import (
    DedupCandidate,
    dice_coefficient,
    open_non_reminder_tasks,
    open_tasks,
)
from app.graph.nodes.complete import (
    _ask_about_unlisted_report,
    _build_completion_match_prompt,
    _celebration_body,
    _choose_completion_target,
    _clarification_body,
    _clarification_candidates,
    _CompletionTarget,
    _deterministic_answer,
    _ledger_options,
    _ledger_targets,
    _parse_names_unlisted,
    _target_from_ledger,
    _task_reference_tokens,
    _TitleMatch,
)


def _target(source: str, page_id: str, title: str = "") -> _CompletionTarget:
    return _CompletionTarget(
        source=source,  # type: ignore[arg-type]
        page_id=page_id,
        task_title=title,
        work_type="",
        energy_required="",
        context_at=None,
    )


# ---------------------------------------------------------------------------
# The gate: messages that name nothing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "incoming",
    [
        "",
        "done",
        "done!",
        "Done!",
        "I did it",
        "yep all done",
        "finished it",
        "ok that's done",
        "done!!! finally",
        "just finished that one",
        "✅",
    ],
)
def test_messages_without_a_task_name_yield_no_tokens(incoming: str) -> None:
    """These resolve from context alone — no Notion read, no model call."""
    assert _task_reference_tokens(incoming) == set()


# ---------------------------------------------------------------------------
# The gate: messages that do name something
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("incoming", "expected_subset"),
    [
        ("done with the dishes", {"dishes"}),
        ("I finished the laundry", {"laundry"}),
        ("finally called the dentist", {"called", "dentist"}),
        ("done, the taxes are submitted", {"taxes", "submitted"}),
        ("finished writing the report", {"writing", "report"}),
    ],
)
def test_messages_naming_a_task_keep_its_words(incoming: str, expected_subset: set[str]) -> None:
    assert expected_subset <= _task_reference_tokens(incoming)


def test_completion_words_never_strip_real_title_words() -> None:
    """Words that are completion-flavored but also real task titles survive.

    Adding any of these to the stopword set would silently re-open the bug for
    every task whose title is built from them.
    """
    residue = _task_reference_tokens("done: call mom, pay rent, clean the sink, sort mail")
    assert {"call", "mom", "pay", "rent", "clean", "sink", "sort", "mail"} <= residue


def test_filler_only_message_shortlists_nothing_downstream() -> None:
    """A false 'names something' costs a Notion read and stops at the shortlist.

    "knocked that out" leaves {knocked}, which overlaps no title, so the
    shortlist is empty and no model call happens.
    """
    residue = _task_reference_tokens("knocked that out")
    assert residue == {"knocked"}
    assert dice_coefficient(residue, {"take", "trash"}) == 0.0


# ---------------------------------------------------------------------------
# No lexical shortcut past the model
# ---------------------------------------------------------------------------

def test_quoting_a_whole_title_is_not_by_itself_a_completion() -> None:
    """Containing a title's every word does not mean the message says it is done.

    "done, now I need to call mom" contains all of "Call mom" while asserting
    the opposite. The shortlist surfaces the candidate either way; only the
    model reading the whole sentence can tell the two apart, so the matcher
    keeps no exact-title fast path around it.
    """
    residue = _task_reference_tokens("done, now I need to call mom")
    title_tokens = _task_reference_tokens("Call mom")
    assert title_tokens <= residue
    assert dice_coefficient(residue, title_tokens) >= 0.30


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------

def test_named_task_outranks_a_different_active_task() -> None:
    """Without this, naming task B while task A is active completes A."""
    active = _target("active_task", "<page_A>")
    title = _target("title_match", "<page_B>")
    chosen = _choose_completion_target(
        active_target=active, recent_target=None, title_target=title
    )
    assert chosen is not None
    assert chosen.page_id == "<page_B>"


def test_named_task_outranks_recent_outbound() -> None:
    recent = _target("recent_outbound", "<page_A>")
    title = _target("title_match", "<page_B>")
    chosen = _choose_completion_target(
        active_target=None, recent_target=recent, title_target=title
    )
    assert chosen is not None
    assert chosen.source == "title_match"


def test_same_page_prefers_the_active_task_for_its_reward_metadata() -> None:
    """active_task is the only source carrying work_type / energy_required."""
    active = _CompletionTarget(
        source="active_task",
        page_id="<page_A>",
        task_title="Fold the laundry",
        work_type="Physical",
        energy_required="Low",
        context_at=None,
    )
    title = _target("title_match", "<page_A>", "Fold the laundry")
    chosen = _choose_completion_target(
        active_target=active, recent_target=None, title_target=title
    )
    assert chosen is not None
    assert chosen.source == "active_task"
    assert chosen.work_type == "Physical"


def test_no_title_match_leaves_existing_precedence_untouched() -> None:
    recent = _target("recent_outbound", "<page_A>")
    active = _target("active_task", "<page_B>")
    chosen = _choose_completion_target(
        active_target=active, recent_target=recent, title_target=None
    )
    assert chosen is not None
    assert chosen.page_id == "<page_A>"


# ---------------------------------------------------------------------------
# Notion write policy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("source", "kind", "event", "reminder_type", "expected"),
    [
        # Tasks always need the write, whichever source found them.
        ("active_task", "task", "suggested", None, True),
        ("title_match", "task", None, None, True),
        ("recent_tasks", "task", "added", None, True),
        ("recent_tasks", "task", "suggested", None, True),
        # A deadline nudge points at a task delivery never completes.
        ("recent_outbound", None, "nudged", "deadline", True),
        ("recent_tasks", "task", "nudged", None, True),
        # A reminder finished before it fired is still Pending in Notion.
        ("title_match", "reminder", None, None, True),
        ("recent_tasks", "reminder", "added", None, True),
        # A delivered reminder write is an idempotent repair if delivery's write failed.
        ("recent_outbound", None, "reminded", "reminder", True),
        ("recent_outbound", None, None, None, True),
        ("recent_tasks", "reminder", "reminded", None, True),
    ],
)
def test_needs_notion_write_is_derived_from_the_source(
    source: str,
    kind: str | None,
    event: str | None,
    reminder_type: str | None,
    expected: bool,
) -> None:
    """Every completion writes Status to Completed, whatever resolved the target.

    Delivery writes Completed when it marks a reminder sent, but that write can
    fail and leave the page Pending; the user's later "done" repairs it, and
    writing Completed to an already-Completed page is a no-op. Deriving the
    flag keeps a caller from constructing a target that skips the write.
    """
    target = _CompletionTarget(
        source=source,  # type: ignore[arg-type]
        page_id="<page_A>",
        task_title="",
        work_type="",
        energy_required="",
        context_at=None,
        kind=kind,  # type: ignore[arg-type]
        event=event,  # type: ignore[arg-type]
        reminder_type=reminder_type,
    )
    assert target.needs_notion_write is expected


# ---------------------------------------------------------------------------
# Prompt contract
# ---------------------------------------------------------------------------

def test_prompt_asks_whether_the_task_is_already_finished() -> None:
    """The question is 'is this done?', not intake's 'is this the same task?'.

    A same-task prompt matches "done, now I need to call mom" against an open
    "Call mom" and completes a task the user just said they still have to do.
    """
    prompt = _build_completion_match_prompt(
        "done, now I need to call mom",
        [DedupCandidate(page_id="<page_A>", title="Call mom", score=0.8)],
    )
    assert "ALREADY FINISHED" in prompt
    assert "still intends to do" in prompt
    assert "<page_A>" in prompt
    assert '{"matched_page_id"' in prompt


def test_prompt_uses_no_bracketed_placeholder_slots() -> None:
    """Bracketed slots read as an instruction to paraphrase (see _task_token)."""
    prompt = _build_completion_match_prompt(
        "done with the dishes",
        [DedupCandidate(page_id="<page_A>", title="Wash the dishes", score=0.9)],
    )
    assert "[task]" not in prompt
    assert "[title]" not in prompt


# ---------------------------------------------------------------------------
# Recent-task ledger anchor
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _entry(page_id: str, event: str, minutes_ago: float | None, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "page_id": page_id,
        "title": extra.pop("title", f"Title {page_id}"),
        "kind": extra.pop("kind", "task"),
        "event": event,
    }
    if minutes_ago is not None:
        entry["at"] = (_NOW - timedelta(minutes=minutes_ago)).isoformat()
    entry.update(extra)
    return entry


def test_a_bare_done_anchors_to_the_task_just_added() -> None:
    """The F1 shape: a reminder added a minute ago, nothing else in context."""
    ledger = [_entry("<page_R>", "added", 1, kind="reminder", title="Take the bins out")]
    target = _target_from_ledger(ledger, now=_NOW)
    assert target is not None
    assert target.page_id == "<page_R>"
    assert target.source == "recent_tasks"
    assert target.task_title == "Take the bins out"
    # Added, not delivered: the reminder page is still Pending and must be written.
    assert target.needs_notion_write is True


@pytest.mark.parametrize("event", ["completed", "rejected"])
def test_a_completion_or_rejection_is_the_last_word(event: str) -> None:
    """A second "done" right after one is an echo, not news about an older task."""
    ledger = [_entry("<page_A>", event, 1), _entry("<page_B>", "added", 30)]
    assert _target_from_ledger(ledger, now=_NOW) is None


def test_an_entry_older_than_a_day_anchors_nothing() -> None:
    ledger = [_entry("<page_A>", "added", 25 * 60)]
    assert _target_from_ledger(ledger, now=_NOW) is None


def test_a_missing_timestamp_reads_as_now_and_a_bad_one_is_dropped() -> None:
    """Static eval fixtures cannot carry a fresh `at`; checkpoints always do."""
    undated = [_entry("<page_A>", "added", None)]
    target = _target_from_ledger(undated, now=_NOW)
    assert target is not None and target.context_at == _NOW

    garbled = [_entry("<page_A>", "added", None, at="not-a-time")]
    assert _target_from_ledger(garbled, now=_NOW) is None


def _ledger_choice(ledger: list[dict[str, Any]], **sources: Any) -> _CompletionTarget | None:
    return _choose_completion_target(
        active_target=sources.get("active"),
        recent_target=sources.get("recent"),
        title_target=sources.get("title"),
        ledger_targets=_ledger_targets(ledger, now=_NOW),
    )


def test_the_ledger_anchor_outranks_an_older_active_task() -> None:
    """A task added a minute ago is the likelier referent than one handed over an hour ago."""
    active = _CompletionTarget(
        source="active_task",
        page_id="<page_A>",
        task_title="Water the plants",
        work_type="",
        energy_required="",
        context_at=_NOW - timedelta(hours=1),
    )
    chosen = _ledger_choice([_entry("<page_R>", "added", 1)], active=active)
    assert chosen is not None and chosen.page_id == "<page_R>"


def test_two_tasks_touched_minutes_apart_are_ambiguous() -> None:
    """Loop 5: neither of two tasks added three minutes apart is a safe guess."""
    ledger = [_entry("<page_A>", "added", 2), _entry("<page_B>", "added", 5)]
    assert _ledger_choice(ledger) is None


def test_ambiguity_spans_sources() -> None:
    """A task suggested five minutes before a reminder was added is just as live."""
    active = _CompletionTarget(
        source="active_task",
        page_id="<page_A>",
        task_title="Water the plants",
        work_type="",
        energy_required="",
        context_at=_NOW - timedelta(minutes=6),
    )
    assert _ledger_choice([_entry("<page_R>", "added", 1)], active=active) is None


def test_the_same_page_from_two_sources_is_one_candidate() -> None:
    """A delivery merged into the ledger is still one task, not an ambiguity."""
    recent = _CompletionTarget(
        source="recent_outbound",
        page_id="<page_R>",
        task_title="Reminder body placeholder",
        work_type="",
        energy_required="",
        context_at=_NOW,
        signal_timestamp=1,
        event="reminded",
        reminder_type="reminder",
    )
    ledger = [_entry("<page_R>", "reminded", 0.5, kind="reminder")]
    chosen = _ledger_choice(ledger, recent=recent)
    assert chosen is not None
    assert chosen.source == "recent_outbound"
    assert chosen.needs_notion_write is True


def test_an_active_task_and_its_suggested_entry_are_one_candidate() -> None:
    """Selection writes both for the same page; that is one task, not an ambiguity.

    The merged target keeps the active task's reward metadata.
    """
    active = _CompletionTarget(
        source="active_task",
        page_id="<page_A>",
        task_title="Water the plants",
        work_type="Physical",
        energy_required="Low",
        context_at=_NOW - timedelta(minutes=1),
        kind="task",
        event="suggested",
    )
    ledger = [_entry("<page_A>", "suggested", 1, title="Water the plants")]
    chosen = _ledger_choice(ledger, active=active)
    assert chosen is not None
    assert chosen.source == "active_task"
    assert chosen.work_type == "Physical"
    assert chosen.needs_notion_write is True


def test_after_a_rejection_the_suggested_alternative_anchors() -> None:
    """Rejection records `rejected` then `suggested` at the same instant."""
    ledger = [
        _entry("<page_B>", "suggested", 1),
        _entry("<page_A>", "rejected", 1),
    ]
    chosen = _ledger_choice(ledger)
    assert chosen is not None and chosen.page_id == "<page_B>"


def test_a_named_task_still_outranks_the_ledger() -> None:
    title = _target("title_match", "<page_B>", "Wash the dishes")
    chosen = _ledger_choice([_entry("<page_A>", "added", 1)], title=title)
    assert chosen is not None and chosen.page_id == "<page_B>"


# ---------------------------------------------------------------------------
# Deterministic acceptance of an answer
# ---------------------------------------------------------------------------

_OPEN = [
    {"id": "<page_R>", "title": "Take the bins out", "kind": "reminder"},
    {"id": "<page_A>", "title": "Water the garden", "kind": "task"},
    {"id": "<page_B>", "title": "Water the lawn", "kind": "task"},
]


def test_an_answer_typed_back_verbatim_is_accepted_without_the_model() -> None:
    residue = _task_reference_tokens("take the bins out")
    match = _deterministic_answer(residue, _OPEN)
    assert match is not None and match["id"] == "<page_R>"


def test_an_answer_that_fits_two_titles_is_left_to_the_model() -> None:
    assert _deterministic_answer(_task_reference_tokens("the water one"), _OPEN) is None


def test_a_partial_answer_is_left_to_the_model() -> None:
    assert _deterministic_answer(_task_reference_tokens("the garden one"), _OPEN) is None


# ---------------------------------------------------------------------------
# Clarification options and wording
# ---------------------------------------------------------------------------

def test_clarification_options_lead_with_the_ledger_and_cap_at_three() -> None:
    ledger = [
        _entry("<page_A>", "added", 1),
        _entry("<page_B>", "suggested", 5),
        # A delivered reminder is already Completed; it cannot be an answer.
        _entry("<page_R>", "reminded", 6, kind="reminder"),
        _entry("<page_C>", "completed", 7),
    ]
    shortlist = _TitleMatch(
        target=None,
        candidate_count=2,
        confidence=None,
        candidates=(
            DedupCandidate("<page_A>", "Title <page_A>", 0.9),
            DedupCandidate("<page_D>", "Title <page_D>", 0.5),
            DedupCandidate("<page_E>", "Title <page_E>", 0.4),
        ),
    )
    options, from_context = _clarification_candidates(
        _ledger_options(ledger, now=_NOW), shortlist
    )
    assert [option.page_id for option in options] == ["<page_A>", "<page_B>", "<page_D>"]
    assert from_context is True


def test_a_widened_scan_adds_no_options_even_with_a_ledger() -> None:
    widened = _TitleMatch(
        target=None,
        candidate_count=1,
        confidence=None,
        candidates=(DedupCandidate("<page_D>", "Title <page_D>", 0.0),),
        widened=True,
    )
    options, _ = _clarification_candidates([], widened)
    assert options == ()


def test_every_ask_family_words_its_two_attempts_differently() -> None:
    options = (DedupCandidate("<page_A>", "Water the garden", 1.0),)
    bodies = {
        _clarification_body(attempt, options, offerable=offerable, from_context=context)
        for attempt in (0, 1)
        for offerable, context in ((True, True), (True, False), (False, False))
    }
    assert len(bodies) == 6


# ---------------------------------------------------------------------------
# Celebration body
# ---------------------------------------------------------------------------

def test_the_celebration_names_the_task_first() -> None:
    assert _celebration_body("Take the bins out", "Nice work! ✨") == "{task} — done. Nice work! ✨"


def test_a_muted_reward_follows_the_name_without_saying_done_twice() -> None:
    assert _celebration_body("Placeholder task", "Done. That mattered.") == (
        "{task} — Done. That mattered."
    )


def test_an_unknown_title_sends_the_reward_text_alone() -> None:
    assert _celebration_body("", "Nice work! ✨") == "Nice work! ✨"


def test_open_tasks_includes_reminders_only_when_asked() -> None:
    def page(page_id: str, *, reminder: bool, status: str = "Pending") -> dict[str, Any]:
        return {
            "id": page_id,
            "properties": {
                "Title": {"title": [{"plain_text": f"Title {page_id}"}]},
                "Status": {"select": {"name": status}},
                "Is Reminder": {"checkbox": reminder},
            },
        }

    response = {"results": [
        page("<page_A>", reminder=False),
        page("<page_R>", reminder=True),
        page("<page_D>", reminder=True, status="Completed"),
    ]}
    assert [(t["id"], t["kind"]) for t in open_tasks(response, include_reminders=True)] == [
        ("<page_A>", "task"),
        ("<page_R>", "reminder"),
    ]
    assert [t["id"] for t in open_non_reminder_tasks(response)] == ["<page_A>"]


# ---------------------------------------------------------------------------
# A report of something on none of the candidates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response_text", "expected"),
    [
        ('{"matched_page_id": null, "confidence": 0.0, "names_unlisted_task": true}', True),
        ('{"matched_page_id": null, "confidence": 0.0, "names_unlisted_task": false}', False),
        # Absent: the older shape reads as no claim.
        ('{"matched_page_id": null, "confidence": 0.0}', False),
        # Only a JSON boolean counts; a string or number is not a claim.
        ('{"matched_page_id": null, "names_unlisted_task": "true"}', False),
        ('{"matched_page_id": null, "names_unlisted_task": 1}', False),
        ("not json at all", False),
        ('{"names_unlisted_task": true', False),
        ('Sure! {"matched_page_id": null, "names_unlisted_task": true} hope that helps', True),
    ],
)
def test_names_unlisted_parser_tolerates_every_shape(response_text: str, expected: bool) -> None:
    assert _parse_names_unlisted(response_text) is expected


def test_the_standalone_prompt_asks_for_the_unlisted_report() -> None:
    candidates = [DedupCandidate("<page_A>", "Call mom", 0.9)]
    standalone = _build_completion_match_prompt("I also paid the gas bill!", candidates)
    answering = _build_completion_match_prompt(
        "the mom one", candidates, answering_clarification=True
    )
    assert '"names_unlisted_task": false' in standalone
    assert "matches none of the candidates" in standalone
    # An answer to "which one?" never names something new.
    assert "names_unlisted_task" not in answering


def test_the_title_match_defaults_to_no_unlisted_report() -> None:
    assert _TitleMatch(target=None, candidate_count=0, confidence=None).names_unlisted is False


def test_an_unlisted_report_offers_the_context_task_by_token() -> None:
    result = _ask_about_unlisted_report(
        "<test-peer>",
        attempts=0,
        options=[
            DedupCandidate("<page_A>", "Fold the laundry", 0.0),
            DedupCandidate("<page_B>", "Book the dentist", 0.0),
            DedupCandidate("<page_C>", "", 0.0),
            DedupCandidate("<page_D>", "Water the plants", 0.0),
            DedupCandidate("<page_E>", "Call the bank", 0.0),
        ],
    )
    draft = result["pending_outbound"][0]
    assert draft["body"] == (
        "Nice one — I don't have that on your list. "
        "Want me to add it as done, or did you mean {task}?"
    )
    assert draft["notion_page_title"] == "Fold the laundry"
    assert draft["notion_page_id"] is None
    assert result["active_task"] is None
    clarification = result["pending_clarification"]
    assert clarification["kind"] == "complete_target"
    assert clarification["attempts"] == 1
    # Only the option the question names is stored: a positional answer can
    # point only at what the user was shown.
    assert [c["page_id"] for c in clarification["candidates"]] == ["<page_A>"]


def test_an_unlisted_report_with_nothing_to_name_asks_to_add_it() -> None:
    result = _ask_about_unlisted_report("<test-peer>", attempts=0, options=[])
    draft = result["pending_outbound"][0]
    assert draft["body"] == "Nice one — I don't have that on your list. Want me to add it?"
    assert "notion_page_title" not in draft
    assert result["pending_clarification"]["candidates"] == []


def test_the_unlisted_copy_never_contrasts_against_the_list() -> None:
    for options in ([], [DedupCandidate("<page_A>", "Fold the laundry", 0.0)]):
        body = _ask_about_unlisted_report("<p>", attempts=0, options=options)[
            "pending_outbound"
        ][0]["body"].lower()
        for phrase in ("but ", "only ", "instead", "not done", "wrong", "didn't"):
            assert phrase not in body
