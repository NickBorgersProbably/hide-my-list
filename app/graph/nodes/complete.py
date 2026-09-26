"""COMPLETE node: task completion + reward integration.

Marks the finished task as completed in Notion, triggers the reward subsystem,
and drafts a celebration message that names the task into pending_outbound.

Four sources can identify which task the user means. A task named in the
message itself comes first, because it is the only source the user can steer.
The other three are context: the recent-task ledger (what this conversation
added, suggested, or was reminded about), the unresolved reminder the assistant
last sent (`recent_outbound`), and the task handed to the user by selection
(`active_task`). Among those the newest wins — unless two different tasks were
touched within minutes of each other, where any pick is a guess and the node
asks instead.

When nothing resolves, the node asks — naming the tasks the conversation just
touched first — and records the question in `pending_clarification` so the
reply that answers it comes back here instead of re-entering cold and
re-asking. A failure in any one source narrows the answer, never the question:
the lookups are independent, so a dead Postgres or an empty shortlist must not
stop the others from running.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

import structlog

from app.graph.context import ledger_entry, record_task_event
from app.graph.nodes._task_match import (
    DedupCandidate,
    dice_coefficient,
    extract_title,
    normalize_title_tokens,
    open_tasks,
    parse_match_response,
    shortlist_duplicate_candidates,
)
from app.graph.nodes._task_token import TASK_TOKEN
from app.graph.state import (
    ActiveTask,
    ClarificationCandidate,
    OutboundDraft,
    PendingClarification,
    RecentTaskEvent,
    RecentTaskKind,
    State,
)

log = structlog.get_logger(__name__)

_ACTIVE_TASK_TTL = timedelta(hours=24)

# A ledger entry anchors a bare "done" for this long. Matches the active-task
# TTL: past a day, "done" is more likely about something else.
_LEDGER_ANCHOR_TTL = timedelta(hours=24)

# Ledger events that leave a task open and in the user's hands. `completed` and
# `rejected` are the conversation's last word on a page, so they anchor nothing.
_LEDGER_ANCHOR_EVENTS: frozenset[str] = frozenset({"added", "suggested", "reminded", "nudged"})

# Two different tasks touched this close together make a bare "done"
# ambiguous: the newer one is not meaningfully more likely than the older. The
# node names both instead of guessing, because a wrong guess writes Completed
# to a task the user has not finished.
_CONTEXT_AMBIGUITY_WINDOW = timedelta(minutes=15)

# Stricter than intake's 0.85. The two failure modes differ in detectability,
# not just direction: intake's false match names the task it matched, so the
# user sees it that turn. A false match here stamps Completed At on a task the
# user has not done and celebrates the one they have — nothing surfaces the
# error until the task fails to reappear days later.
_TITLE_MATCH_CONFIDENCE_THRESHOLD = 0.90

# Word-overlap bar for accepting an answer to "which task did you mean?"
# without a model call. Only ever applied to an answer: the completion claim
# was made on the previous turn, so the answer only has to name one task, and
# typing a title back nearly verbatim does exactly that. A standalone message
# never takes this path — "done, now I need to call mom" contains every word
# of "Call mom" while saying it is not done.
_DETERMINISTIC_ANSWER_THRESHOLD = 0.85

# Below the shortlist default (0.4) because a message names a task in fewer
# words than the title carries: {laundry} against "Fold the laundry before bed"
# scores exactly 0.4 and would sit on the boundary. The score is a recall
# device for building the model's candidate set — it never authorizes a write.
_TITLE_MATCH_MIN_SCORE = 0.30

# When nothing clears _TITLE_MATCH_MIN_SCORE, the open list goes to the model
# anyway, ranked but unfiltered. Token overlap answers "do these two strings
# share words", and the question here is "does this message report this task as
# done" — the two come apart exactly where a user paraphrases their own task,
# which is the normal way to describe finishing something. A zero score is
# evidence that the words differ, not evidence that the task is unrelated, so
# it may not decide on its own that the model never sees the list.
#
# The cap bounds the prompt, not the recall: it only binds on lists longer than
# this, and complete_node.candidate_set_truncated says when it did.
_FALLBACK_CANDIDATE_LIMIT = 40

# Re-asks before the agent stops asking. The first question is open ("which
# task did you mean?"); the second names concrete options, per
# design/adhd-priorities.md — "if you must ask one question, offer 2-3
# constrained choices, not open-ended". After that the agent stops rather than
# spending more of the user's attention on a question that is not landing.
_MAX_CLARIFICATION_ATTEMPTS = 2

# Options named in a single re-ask. Recognition beats recall, but a long list
# is its own decision load.
_CLARIFICATION_OPTION_LIMIT = 3

# Subtracted from the user's message only, never from a task title. These words
# say "I finished something" without saying which something; leaving them in
# lets a message that names no task shortlist a task anyway. Kept deliberately
# small: a word wrongly included here re-opens the bug this path exists to fix,
# while a word wrongly omitted costs one Notion read that shortlists nothing.
# Words like "call", "clean", "pay", and "sort" are excluded for that reason —
# they are completion-flavored but they are also real task titles.
_COMPLETION_WORDS: frozenset[str] = frozenset({
    "done", "did", "doing", "finish", "finished", "finishing",
    "complete", "completed", "completing",
    "yep", "yeah", "yup", "yes", "ok", "okay", "sure",
    "that", "thats", "this", "these", "those", "one", "ones", "them", "they",
    "task", "tasks", "thing", "things", "item", "items", "list",
    "just", "now", "all", "already", "finally", "got", "have", "ive", "im",
    "out", "up", "off",
})

_KINDS: frozenset[str] = frozenset({"task", "reminder"})

CompletionSource = Literal["active_task", "recent_outbound", "title_match", "recent_tasks"]


@dataclass(frozen=True)
class _CompletionTarget:
    source: CompletionSource
    page_id: str
    task_title: str
    work_type: str
    energy_required: str
    context_at: datetime | None
    signal_timestamp: int | None = None
    # What the page is and what last happened to it. None means "not known
    # from this source"; `resolved_kind` fills the gap from the source.
    kind: RecentTaskKind | None = None
    event: RecentTaskEvent | None = None
    # recent_outbound.reminder_type — the outbox row's kind, for a delivery.
    reminder_type: str | None = None

    @property
    def resolved_kind(self) -> RecentTaskKind:
        """The page's kind, from the source when no source said so outright.

        A recent_outbound row carries its outbox kind in `reminder_type`: a
        `reminder` row points at a reminder page, a `deadline` row at a task.
        """
        if self.kind is not None:
            return self.kind
        if self.source == "recent_outbound" and self.reminder_type != "deadline":
            return "reminder"
        return "task"

    @property
    def delivered_reminder(self) -> bool:
        """Whether this is a reminder page the worker has already delivered.

        Delivery completes a reminder page (`complete_reminder`), so a
        delivered one is already Completed in Notion. A reminder the user
        finishes before it fires is still Pending, and a deadline nudge points
        at a task delivery never touches — both still need the write.
        """
        if self.resolved_kind != "reminder":
            return False
        if self.source == "recent_outbound":
            return self.reminder_type != "deadline"
        return self.event == "reminded"

    @property
    def needs_notion_write(self) -> bool:
        """Whether completing this target still has to write Status to Notion.

        Always True. Delivery writes Completed when it marks the reminder sent,
        but if that write fails the page stays Pending. The user's later "done"
        repairs it idempotently: writing Completed to an already-Completed page
        is a no-op. A settable field would let a caller skip or repeat the
        write; keeping this derived prevents that.
        """
        return True


@dataclass(frozen=True)
class _TitleMatch:
    """Outcome of resolving the user's message against open Notion tasks."""

    target: _CompletionTarget | None
    candidate_count: int
    confidence: float | None
    candidates: tuple[DedupCandidate, ...] = ()
    # True when the candidate set is the whole open list rather than titles the
    # message actually overlaps. It changes what a null match means: over scored
    # candidates the model is saying "the task you named is not done", which is
    # a reason to stop; over the whole list it is saying "I could not tell",
    # which is not.
    widened: bool = False
    # True when an answer to a clarification matched a title on word overlap
    # alone and the model was not asked.
    deterministic: bool = False


@dataclass(frozen=True)
class _LedgerItem:
    """One validated ledger entry, with its timestamp parsed."""

    page_id: str
    title: str
    kind: RecentTaskKind
    event: RecentTaskEvent
    at: datetime


def _parse_checkpoint_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _target_from_active_task(
    active_task: ActiveTask | None,
    *,
    now: datetime,
) -> _CompletionTarget | None:
    if not active_task:
        return None

    page_id = active_task.get("page_id", "")
    if not page_id:
        return None

    selected_at_value = active_task.get("selected_at")
    selected_at = _parse_checkpoint_datetime(selected_at_value)
    if selected_at is None:
        log.info(
            "complete_node.active_task_missing_selected_at",
            page_id=page_id,
            has_selected_at=bool(selected_at_value),
        )
        return None
    if now - selected_at > _ACTIVE_TASK_TTL:
        log.info(
            "complete_node.active_task_stale",
            page_id=page_id,
            selected_at=selected_at.isoformat(),
        )
        return None

    return _CompletionTarget(
        source="active_task",
        page_id=page_id,
        # `.get(key, default)` only fires on a missing key, so a stored empty
        # title used to reach the reward path verbatim. task_title is private
        # data written to the manifest — pass the empty string through rather
        # than fabricating a placeholder that would be stored as if it were the
        # user's own words.
        task_title=(active_task.get("title") or "").strip(),
        work_type=active_task.get("work_type", ""),
        energy_required=active_task.get("energy_required", ""),
        context_at=selected_at,
        kind="task",
        event="suggested",
    )


def _ledger_items(recent_tasks: Sequence[object] | None, *, now: datetime) -> list[_LedgerItem]:
    """Validate the ledger and return it newest first.

    Checkpointed entries always carry `at`. A static eval fixture cannot carry
    a fresh timestamp, so an entry with no `at` at all is read as happening
    now; an entry whose `at` is present but unparseable is dropped, as is any
    entry with an unknown kind or event.
    """
    items: list[_LedgerItem] = []
    for raw in recent_tasks or []:
        if not isinstance(raw, Mapping):
            continue
        page_id = raw.get("page_id")
        kind = raw.get("kind")
        event = raw.get("event")
        if not isinstance(page_id, str) or not page_id:
            continue
        if kind not in _KINDS or event not in _LEDGER_ANCHOR_EVENTS | {"completed", "rejected"}:
            continue
        raw_at = raw.get("at")
        at = now if not raw_at else _parse_checkpoint_datetime(raw_at)
        if at is None:
            continue
        title = raw.get("title")
        items.append(
            _LedgerItem(
                page_id=page_id,
                title=title.strip() if isinstance(title, str) else "",
                kind=cast(RecentTaskKind, kind),
                event=cast(RecentTaskEvent, event),
                at=at,
            )
        )
    return sorted(items, key=lambda item: item.at, reverse=True)


def _ledger_target(item: _LedgerItem) -> _CompletionTarget:
    return _CompletionTarget(
        source="recent_tasks",
        page_id=item.page_id,
        task_title=item.title,
        work_type="",
        energy_required="",
        context_at=item.at,
        kind=item.kind,
        event=item.event,
    )


def _ledger_targets(
    recent_tasks: Sequence[object] | None, *, now: datetime
) -> list[_CompletionTarget]:
    """Ledger entries that can anchor a bare "done", newest first.

    Nothing anchors when the ledger's newest entry is a completion or a
    rejection: that was the conversation's last word, and a second "done"
    straight after one is more likely an echo than news about an older task.
    Otherwise every open-event entry from the last `_LEDGER_ANCHOR_TTL` is
    returned, so the caller can tell a clear anchor from two tasks touched
    moments apart.
    """
    items = _ledger_items(recent_tasks, now=now)
    if not items or items[0].event not in _LEDGER_ANCHOR_EVENTS:
        return []
    return [
        _ledger_target(item)
        for item in items
        if item.event in _LEDGER_ANCHOR_EVENTS and now - item.at <= _LEDGER_ANCHOR_TTL
    ]


def _target_from_ledger(
    recent_tasks: Sequence[object] | None, *, now: datetime
) -> _CompletionTarget | None:
    """The newest ledger entry that anchors a bare "done", or None."""
    targets = _ledger_targets(recent_tasks, now=now)
    return targets[0] if targets else None


def _ledger_options(
    recent_tasks: Sequence[object] | None, *, now: datetime
) -> list[DedupCandidate]:
    """Ledger tasks worth naming when the node has to ask, newest first.

    Open-event entries with a known title. A delivered reminder is left out:
    delivery already completed its page, so the answering turn could not find
    it among the open tasks and the option would name something unanswerable.
    """
    options: list[DedupCandidate] = []
    for item in _ledger_items(recent_tasks, now=now):
        if item.event not in _LEDGER_ANCHOR_EVENTS or not item.title:
            continue
        if item.kind == "reminder" and item.event == "reminded":
            continue
        options.append(DedupCandidate(page_id=item.page_id, title=item.title, score=1.0))
    return options


async def _load_recent_outbound_target(peer: str) -> _CompletionTarget | None:
    """The newest delivery awaiting a reply, as a completion target."""
    from app.tools import reminders

    row = await reminders.load_recent_outbound(peer)
    if not row:
        return None

    signal_timestamp = int(row["signal_timestamp"])
    sent_at = row["sent_at"]
    if not isinstance(sent_at, datetime):
        sent_at = _parse_checkpoint_datetime(sent_at)
    elif sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=UTC)
    else:
        sent_at = sent_at.astimezone(UTC)

    reminder_type = str(row.get("reminder_type") or "reminder")
    return _CompletionTarget(
        source="recent_outbound",
        page_id=str(row["notion_page_id"]),
        # The sent body, not the task title. It is a reward-classification
        # fallback only; the celebration and the ledger never use it.
        task_title=str(row.get("title") or "").strip(),
        work_type="",
        energy_required="",
        context_at=sent_at,
        signal_timestamp=signal_timestamp,
        event="nudged" if reminder_type == "deadline" else "reminded",
        reminder_type=reminder_type,
    )


def _task_reference_tokens(incoming: str) -> set[str]:
    """Return the tokens in `incoming` that could name a task.

    Empty means the message reports a completion without saying which task —
    "done!", "I did it" — and the caller resolves from context instead. A
    non-empty result only earns a lookup, never a write.
    """
    return normalize_title_tokens(incoming) - _COMPLETION_WORDS


def _build_completion_match_prompt(
    incoming: str,
    candidates: list[DedupCandidate],
    *,
    answering_clarification: bool = False,
    offered: tuple[DedupCandidate, ...] = (),
) -> str:
    """Ask the model which candidate the message resolves to.

    Two framings, because the message means different things in the two cases.
    A standalone completion has to carry the claim itself, so the model is told
    to reject anything that does not assert a task is finished. An answer to
    "which task did you mean?" carries no such claim and never will: the user
    already said they finished something on the previous turn and was asked
    only *which*. Judging "the garden one" against the standalone rule rejects
    it every time — correctly, for a rule that does not apply to it.

    `offered` is the option list the previous turn actually named, in the order
    it named them. Offering "was it A, B, or C?" and then being unable to read
    "the second one" is worse than never offering choices: it invites the short
    answer and then demands the long one. Enumerating them here is what gives
    an ordinal a referent.
    """
    candidate_payload = [
        {"id": candidate.page_id, "title": candidate.title}
        for candidate in candidates
    ]
    if answering_clarification:
        instructions = (
            "The user reported finishing a task and was asked which one they "
            "meant. This message is their answer.\n\n"
            "Decide which candidate task the answer identifies. The answer does "
            "not need to say the task is done — that was already said on the "
            "previous turn. It only has to point at a task, by name, by "
            "paraphrase, or by some detail that distinguishes it.\n\n"
            "Match only when the answer points at exactly one candidate. If it "
            "identifies none of them, points at several equally well, or "
            "changes the subject, return no match. The cost of a false match is "
            "high: it marks a task the user has not finished as completed. If "
            "uncertain, return no match."
        )
        if offered:
            numbered = "\n".join(
                f"{index}. {candidate.title}"
                for index, candidate in enumerate(offered, start=1)
            )
            instructions += (
                "\n\nThese options were named to the user, in this order:\n"
                f"{numbered}\n"
                'An answer that picks by position — "the first one", "the '
                'second", "the last one" — refers to this numbering. Resolve it '
                "to that option's id."
            )
        shape = '{"matched_page_id": "<candidate id or null>", "confidence": 0.0}'
    else:
        instructions = (
            "The user sent a message reporting that they finished something. "
            "Decide which candidate task, if any, the message says is ALREADY "
            "FINISHED.\n\n"
            "Match only when the message asserts that candidate is done. A "
            "message that mentions a task the user still intends to do, is "
            "asking about, or is about to start is NOT a match — return no "
            "match for those even when the wording overlaps a candidate title. "
            "The cost of a false match is high: it marks a task the user has "
            "not finished as completed. If uncertain, return no match."
        )
        shape = '{"matched_page_id": "<candidate id or null>", "confidence": 0.0}'
    return (
        f"{instructions}\n\n"
        f"User message: {incoming!r}\n"
        f"Candidates: {json.dumps(candidate_payload, ensure_ascii=True)}\n\n"
        "Return JSON only in this shape:\n"
        f"{shape}"
    )


def _reoffer_candidates(
    offered: tuple[ClarificationCandidate, ...],
    open_list: list[Mapping[str, str]],
) -> list[DedupCandidate]:
    """Rebuild the previous turn's options, in the order they were offered.

    Filtered against the current open list and re-read from it, so an option
    that has since been completed or renamed cannot come back through a stale
    checkpoint. Order is the offered order because that is what an ordinal
    answer refers to; the score is unused here and carries no ranking claim.
    """
    open_titles = {task["id"]: task["title"] for task in open_list}
    rebuilt: list[DedupCandidate] = []
    seen: set[str] = set()
    for option in offered:
        if not isinstance(option, dict):
            continue
        page_id = option.get("page_id", "")
        if not page_id or page_id in seen or page_id not in open_titles:
            continue
        seen.add(page_id)
        rebuilt.append(DedupCandidate(page_id=page_id, title=open_titles[page_id], score=0.0))
    return rebuilt


def _deterministic_answer(
    residue: set[str], open_list: list[Mapping[str, str]]
) -> Mapping[str, str] | None:
    """The one open task an answer names nearly verbatim, or None.

    Compares the answer's task-naming words against each title's, with the
    completion words removed from both sides so "take the bins out" and "Take
    the bins out" compare equal. Accepts only a unique hit at or above
    `_DETERMINISTIC_ANSWER_THRESHOLD`; two hits are an ambiguity for the model
    to read, not a coin toss. The caller uses this only for an answer to a
    clarification — never for a standalone message.
    """
    if not residue:
        return None
    hits = [
        task
        for task in open_list
        if (title_tokens := _task_reference_tokens(task.get("title", "")))
        and dice_coefficient(residue, title_tokens) >= _DETERMINISTIC_ANSWER_THRESHOLD
    ]
    return hits[0] if len(hits) == 1 else None


async def _resolve_title_match(
    incoming: str,
    residue: set[str],
    *,
    now: datetime,
    answering_clarification: bool = False,
    offered: tuple[ClarificationCandidate, ...] = (),
) -> _TitleMatch:
    """Resolve a task named in the message against open Notion tasks.

    Fail-soft by design: every failure returns no target so the caller falls
    through to context-based resolution. This must not raise — the node's outer
    handler emits complete_node.error, which the eval runner treats as the
    hand-written fallback path rather than real behavior.

    Scope: the candidate set is every open task, reminder pages included,
    unfiltered by peer. An open reminder page is one that has not fired yet;
    a user can finish it before it does. The peer scope is deliberate and is
    the documented data model, not a missing authorization check — the Notion
    database holds one person's tasks and has no owner column, and
    AUTHORIZED_PEERS lists that person's own addresses. See "Scope and
    Ownership" in docs/notion-schema.md. The access boundary is the allowlist
    at the Signal ingress; past it there is nothing to partition.
    """
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.models import llm
        from app.tools import notion

        raw = await notion.query_all()
        open_list = open_tasks(raw, include_reminders=True)
        kinds = {task["id"]: task.get("kind", "task") for task in open_list}

        def target_for(page_id: str, title: str) -> _CompletionTarget:
            kind: RecentTaskKind = "reminder" if kinds.get(page_id) == "reminder" else "task"
            return _title_target(page_id, title, now=now, kind=kind)

        # The options the previous turn named lead the list, in that order, and
        # are never dropped by ranking — an ordinal answer has no other referent,
        # and a message like "the second one" scores nothing against any title.
        reoffered = (
            _reoffer_candidates(offered, open_list) if answering_clarification else []
        )

        if answering_clarification:
            # An answer that types a title back nearly verbatim has named it.
            # Asking the model to confirm that costs a call and can only add a
            # way to get it wrong.
            answer = _deterministic_answer(residue, open_list)
            if answer is not None:
                log.info(
                    "complete_node.deterministic_answer",
                    page_id=answer["id"],
                    open_task_count=len(open_list),
                )
                return _TitleMatch(
                    target=target_for(answer["id"], answer["title"]),
                    candidate_count=len(open_list),
                    confidence=1.0,
                    candidates=tuple(reoffered),
                    deterministic=True,
                )

        # Every candidate goes to the model, even one that quotes a title
        # verbatim. Containing a task's words is not the same as saying it is
        # finished: "done, now I need to call mom" contains all of "Call mom"
        # while asserting the opposite. Only the whole sentence separates them,
        # so there is no lexical shortcut past this call for a standalone
        # message.
        candidates = shortlist_duplicate_candidates(
            incoming,
            open_list,
            min_score=_TITLE_MATCH_MIN_SCORE,
            query_stopwords=_COMPLETION_WORDS,
        )
        widened = False
        if not candidates:
            # The message named something the shortlist could not place. Rank
            # the whole open list and let the model read it rather than
            # answering "which task did you mean?" without having looked.
            widened = True
            candidates = shortlist_duplicate_candidates(
                incoming,
                open_list,
                limit=_FALLBACK_CANDIDATE_LIMIT,
                min_score=0.0,
                query_stopwords=_COMPLETION_WORDS,
            )
            if len(open_list) > _FALLBACK_CANDIDATE_LIMIT:
                log.info(
                    "complete_node.candidate_set_truncated",
                    open_task_count=len(open_list),
                    limit=_FALLBACK_CANDIDATE_LIMIT,
                )

        if reoffered:
            reoffered_ids = {candidate.page_id for candidate in reoffered}
            candidates = reoffered + [
                candidate for candidate in candidates
                if candidate.page_id not in reoffered_ids
            ]

        if not candidates:
            return _TitleMatch(target=None, candidate_count=0, confidence=None)

        def outcome(
            target: _CompletionTarget | None,
            confidence: float | None,
        ) -> _TitleMatch:
            """Attach the candidate set to every verdict, matched or not.

            The rejected candidates are what the next turn's question offers as
            options, so they have to survive a null match.
            """
            return _TitleMatch(
                target=target,
                candidate_count=len(candidates),
                confidence=confidence,
                candidates=tuple(candidates),
                widened=widened,
            )

        model = llm("cheap", caller="complete_title_match")
        response = await model.ainvoke([
            SystemMessage(content=_build_completion_match_prompt(
                incoming,
                candidates,
                answering_clarification=answering_clarification,
                offered=tuple(reoffered),
            )),
            HumanMessage(content="Return only the JSON object."),
        ])
        parsed = parse_match_response(str(response.content), candidates)
        if parsed is None:
            return outcome(None, None)

        page_id, confidence = parsed
        if confidence < _TITLE_MATCH_CONFIDENCE_THRESHOLD:
            return outcome(None, confidence)

        matches = [candidate for candidate in candidates if candidate.page_id == page_id]
        if len(matches) != 1:
            return outcome(None, confidence)

        return outcome(target_for(matches[0].page_id, matches[0].title), confidence)
    except Exception:
        # Counts only — the message and titles are the user's private words.
        log.warning(
            "complete_node.title_match_failed",
            residue_token_count=len(residue),
            exc_info=True,
        )
        return _TitleMatch(target=None, candidate_count=0, confidence=None)


def _title_target(
    page_id: str, title: str, *, now: datetime, kind: RecentTaskKind = "task"
) -> _CompletionTarget:
    return _CompletionTarget(
        source="title_match",
        page_id=page_id,
        task_title=title,
        work_type="",
        energy_required="",
        context_at=now,
        kind=kind,
    )


# Attempts at cancelling a finished reminder's outbox rows before giving up.
_REMINDER_CANCEL_ATTEMPTS = 2


async def _cancel_pending_reminders(peer: str, page_id: str) -> None:
    """Stop a reminder the user already finished from firing.

    Tries twice. When both attempts fail the completion still stands — the
    Notion write already happened, and taking back the celebration would
    punish the user for a database hiccup. The failure is logged and raised to
    the operator as an ops alert, and the delivery worker's pre-send check
    (`reminder_worker`) skips a reminder whose page is already Completed, so a
    surviving outbox row still does not reach the user.
    """
    from app.tools import ops_alerts, reminders

    last_error: Exception | None = None
    for _ in range(_REMINDER_CANCEL_ATTEMPTS):
        try:
            await reminders.cancel_pending_reminders(peer=peer, notion_page_id=page_id)
            return
        except Exception as exc:
            last_error = exc

    log.warning(
        "complete_node.reminder_cancel_failed",
        page_id=page_id,
        error_type=type(last_error).__name__,
        attempts=_REMINDER_CANCEL_ATTEMPTS,
    )
    try:
        # Placeholder only: an ops alert body never carries a page id or title.
        await ops_alerts.enqueue(
            kind="reminder_cancel_failed",
            body=(
                "Reminder cancellation failed after the user completed <page_id>; "
                "the worker's pre-send check is the remaining guard."
            ),
            severity="warning",
        )
    except Exception as exc:
        log.warning(
            "complete_node.reminder_cancel_alert_failed",
            page_id=page_id,
            error_type=type(exc).__name__,
        )


def _merge_same_page(group: list[_CompletionTarget]) -> _CompletionTarget:
    """Collapse one page's targets from several sources into one.

    The active task wins as the base when present — it is the only source
    carrying work_type and energy_required for the reward call. Otherwise the
    delivery row wins over the ledger entry, because it is what the ledger's
    `reminded`/`nudged` entry was merged from and it says authoritatively
    whether delivery already completed the page. The group's newest timestamp
    stands for the page in arbitration.
    """
    priority = {"active_task": 0, "recent_outbound": 1, "recent_tasks": 2, "title_match": 3}
    base = min(group, key=lambda target: priority[target.source])
    stamps = [target.context_at for target in group if target.context_at is not None]
    if stamps and base.context_at != max(stamps):
        base = replace(base, context_at=max(stamps))
    if base.source == "active_task":
        delivery = next((t for t in group if t.source == "recent_outbound"), None)
        if delivery is not None:
            base = replace(base, signal_timestamp=delivery.signal_timestamp)
    return base


def _context_pool(
    *,
    active_target: _CompletionTarget | None,
    recent_target: _CompletionTarget | None,
    ledger_targets: Sequence[_CompletionTarget] = (),
) -> list[_CompletionTarget]:
    """Every live context target, one per page, newest first.

    Ties go to the delivery, then the active task, then the ledger: the sort
    is stable, so insertion order breaks them. A reminder the user is replying
    to is the likelier referent than a task handed over at the same moment.
    """
    by_page: dict[str, list[_CompletionTarget]] = {}
    for target in (recent_target, active_target, *ledger_targets):
        if target is not None and target.page_id:
            by_page.setdefault(target.page_id, []).append(target)
    merged = [_merge_same_page(group) for group in by_page.values()]
    oldest = datetime.min.replace(tzinfo=UTC)
    return sorted(merged, key=lambda target: target.context_at or oldest, reverse=True)


def _is_ambiguous(pool: Sequence[_CompletionTarget]) -> bool:
    """Whether the two newest context tasks are too close together to pick one."""
    if len(pool) < 2:
        return False
    newest, runner_up = pool[0].context_at, pool[1].context_at
    if newest is None or runner_up is None:
        return False
    return newest - runner_up < _CONTEXT_AMBIGUITY_WINDOW


def _choose_completion_target(
    *,
    active_target: _CompletionTarget | None,
    recent_target: _CompletionTarget | None,
    title_target: _CompletionTarget | None = None,
    ledger_targets: Sequence[_CompletionTarget] = (),
) -> _CompletionTarget | None:
    if title_target:
        # Same page from two sources: keep the active task, which is the only
        # one carrying work_type and energy_required for the reward call.
        if active_target and active_target.page_id == title_target.page_id:
            return active_target
        # The user named a task. That outranks every inference — including an
        # active task pointing at a different page, which would otherwise mark
        # the wrong one done.
        return title_target
    pool = _context_pool(
        active_target=active_target,
        recent_target=recent_target,
        ledger_targets=ledger_targets,
    )
    if not pool or _is_ambiguous(pool):
        return None
    return pool[0]


def _format_options(titles: list[str]) -> str:
    """Render task titles as a natural inline list."""
    if len(titles) == 1:
        return titles[0]
    if len(titles) == 2:
        return f"{titles[0]} or {titles[1]}"
    return f"{', '.join(titles[:-1])}, or {titles[-1]}"


def _clarification_body(
    attempts: int,
    candidates: tuple[DedupCandidate, ...],
    *,
    offerable: bool,
    from_context: bool = False,
) -> str:
    """Compose the question for this attempt.

    When there are candidates worth naming the ask names them — recognition
    rather than recall. Options drawn from what the conversation just touched
    get their own wording, since the user named nothing and the question is
    only which of those it was. Otherwise it stays open, and the second ask
    rephrases rather than repeating, because a message repeated verbatim is the
    failure this whole path exists to prevent.
    """
    if candidates and offerable:
        titles = [candidate.title for candidate in candidates[:_CLARIFICATION_OPTION_LIMIT]]
        options = _format_options(titles)
        if from_context:
            if attempts == 0:
                return f"Nice — which task was it: {options}?"
            return f"Just checking which one — {options}?"
        if attempts == 0:
            return f"I can mark that done — was it {options}?"
        return f"Still not sure which one — was it {options}?"

    if attempts == 0:
        return "I can mark that done. Which task did you mean?"
    return "Still not placing it — what's the task called?"


def _clarification_candidates(
    context_options: Sequence[DedupCandidate], title_match: _TitleMatch
) -> tuple[tuple[DedupCandidate, ...], bool]:
    """Options for the next ask: the conversation's own tasks first, then the shortlist.

    Returns `(candidates, from_context)`. The shortlist joins only when the
    message's words put those titles on it; a widened whole-list scan is not a
    shortlist. `from_context` is true when the leading option came from the
    conversation rather than the message.
    """
    merged: list[DedupCandidate] = []
    seen: set[str] = set()
    shortlist = () if title_match.widened else title_match.candidates
    for candidate in (*context_options, *shortlist):
        if candidate.page_id in seen or not candidate.title:
            continue
        seen.add(candidate.page_id)
        merged.append(candidate)
        if len(merged) >= _CLARIFICATION_OPTION_LIMIT:
            break
    from_context = bool(merged) and bool(context_options) and (
        merged[0].page_id == context_options[0].page_id
    )
    return tuple(merged), from_context


def _clarify_completion_target(
    peer: str,
    *,
    attempts: int = 0,
    candidates: tuple[DedupCandidate, ...] = (),
    offerable: bool = False,
    from_context: bool = False,
) -> dict[str, Any]:
    """Ask which task was meant, and remember having asked.

    `attempts` is the number of times this question has already gone out in the
    current exchange. Past _MAX_CLARIFICATION_ATTEMPTS the agent stops asking
    and leaves the tasks open — an unanswered question re-sent a third time
    costs the user attention and returns nothing.

    `offerable` says whether the candidates are worth presenting as choices —
    true only when there is a reason behind them: the conversation just touched
    them, or the message's own words put them there. A widened candidate set is
    the whole open list ranked by scores that are all effectively zero, so its
    top three are not a shortlist, they are the first three tasks. Naming them
    would present noise as a suggestion, and because a named option can be
    answered by position, the user could pick one and complete a task chosen at
    random. Choices are only a kindness when there is a reason behind them.
    """
    if attempts >= _MAX_CLARIFICATION_ATTEMPTS:
        log.info(
            "complete_node.clarification_exhausted",
            has_peer=bool(peer),
            attempts=attempts,
            candidate_count=len(candidates),
        )
        give_up_draft: OutboundDraft = {
            "recipient": peer,
            "body": (
                "No problem — I'll leave those as they are. "
                "Send me the task name whenever you want it marked done."
            ),
            "notion_page_id": None,
        }
        return {
            "pending_outbound": [give_up_draft],
            "conversation_state": "idle",
            "active_task": None,
            "pending_clarification": None,
        }

    # Titles are the user's private words: store them in checkpointed state so
    # the re-ask can name them, and log counts only. Only offerable candidates
    # are stored — an option the user was never shown must not become the
    # referent of "the first one" on the next turn.
    stored: list[ClarificationCandidate] = [
        {"page_id": candidate.page_id, "title": candidate.title}
        for candidate in (candidates[:_CLARIFICATION_OPTION_LIMIT] if offerable else ())
    ]
    clarification: PendingClarification = {
        "kind": "complete_target",
        "asked_at": datetime.now(UTC).isoformat(),
        "attempts": attempts + 1,
        "candidates": stored,
    }
    no_task_draft: OutboundDraft = {
        "recipient": peer,
        "body": _clarification_body(
            attempts, candidates, offerable=offerable, from_context=from_context
        ),
        "notion_page_id": None,
    }
    log.info(
        "complete_node.clarification_asked",
        has_peer=bool(peer),
        attempts=attempts + 1,
        named_option_count=len(stored),
        options_from_context=from_context and bool(stored),
    )
    return {
        "pending_outbound": [no_task_draft],
        "conversation_state": "idle",
        "active_task": None,
        "pending_clarification": clarification,
    }


async def _resolve_display_title(
    target: _CompletionTarget, recent_tasks: Sequence[object]
) -> str:
    """The stored task title to name in the celebration, or "" if unknown.

    Never the sent reminder body: a recent_outbound target's `task_title` is
    the message that went out, not the task. The ledger's title comes first,
    then the page itself. The Notion read is fail-soft — a lookup failure costs
    the name, never the completion.
    """
    if target.source != "recent_outbound" and target.task_title:
        return target.task_title
    known = ledger_entry(recent_tasks, target.page_id)
    if known and known["title"]:
        return known["title"]
    try:
        from app.tools import notion

        page = await notion.get_page(page_id=target.page_id)
        props = page.get("properties", {}) if isinstance(page, dict) else {}
        return extract_title(props if isinstance(props, dict) else {}).strip()
    except Exception as exc:
        log.warning(
            "complete_node.title_lookup_failed",
            page_id=target.page_id,
            error_type=type(exc).__name__,
        )
        return ""


def _celebration_body(title: str, reward_text: str) -> str:
    """Name the task, then celebrate it.

    The body leads with the `{task}` token and the draft carries the title, so
    `send_node` substitutes the exact stored title. A reward text that already
    opens with "Done" (the muted sensitive-task text among them) follows the
    name directly rather than saying done twice. With no known title the reward
    text goes out alone.
    """
    if not title:
        return reward_text
    if reward_text.lstrip().lower().startswith("done"):
        return f"{TASK_TOKEN} — {reward_text}"
    return f"{TASK_TOKEN} — done. {reward_text}"


async def complete_node(state: State) -> dict[str, Any]:
    """COMPLETE handler: update Notion, call rewards.maybe_reward(), draft reply."""
    peer = state.get("peer", "")

    try:
        from app.tools import notion, reminders
        from app.tools.rewards import maybe_reward

        active_task = state.get("active_task")
        now = datetime.now(UTC)
        active_target = _target_from_active_task(active_task, now=now)
        recent_tasks = list(state.get("recent_tasks") or [])
        ledger_targets = _ledger_targets(recent_tasks, now=now)

        # Attempts already spent on this question. Absent for a first "done",
        # present when this turn is the answer to a clarification classify_intent
        # kept alive.
        # classify_intent only carries a clarification forward while it is live,
        # so its presence means this turn is the answer to one. That changes how
        # the message reads: the completion claim was made on the previous turn
        # and this message only says which task it was about.
        pending = state.get("pending_clarification")
        answering = isinstance(pending, dict) and bool(pending)
        raw_attempts = pending.get("attempts", 0) if isinstance(pending, dict) else 0
        attempts = raw_attempts if isinstance(raw_attempts, int) and raw_attempts >= 0 else 0
        raw_offered = pending.get("candidates") if isinstance(pending, dict) else None
        offered: tuple[ClarificationCandidate, ...] = (
            tuple(raw_offered) if answering and isinstance(raw_offered, list) else ()
        )

        try:
            recent_target = await _load_recent_outbound_target(peer)
        except Exception:
            # Losing one source must not veto the others: the message may still
            # name the task outright, and that path does not touch Postgres.
            log.warning(
                "complete_node.recent_outbound_load_failed",
                active_page_id=active_target.page_id if active_target else None,
                exc_info=True,
            )
            recent_target = None

        # Only look the message up when it names something. "done!" resolves
        # from context alone and must not pay for a Notion read or a model call.
        residue = _task_reference_tokens(state.get("incoming") or "")
        # Answering a question we named options in earns the lookup on its own.
        # "the second one" reduces to no residue worth shortlisting, and it is
        # still a complete answer to what was asked.
        if residue or offered:
            title_match = await _resolve_title_match(
                state.get("incoming") or "",
                residue,
                now=now,
                answering_clarification=answering,
                offered=offered,
            )
        else:
            title_match = _TitleMatch(target=None, candidate_count=0, confidence=None)

        context_options = _ledger_options(recent_tasks, now=now)
        clarify_candidates, from_context = _clarification_candidates(
            context_options, title_match
        )

        target = _choose_completion_target(
            active_target=active_target,
            recent_target=recent_target,
            title_target=title_match.target,
            ledger_targets=ledger_targets,
        )

        # When the message appeared to name a task (candidates the message
        # actually overlaps) but the model rejected all of them, the message
        # asserts something is NOT done — completing from stale context would
        # mark the wrong page.
        #
        # Scoped to the scored shortlist. A widened set contains every open
        # task, so a null match over it carries no claim about any particular
        # one: "done :) feeling good" would otherwise veto a live active task
        # on the strength of a candidate list the message never referred to.
        if (
            target
            and residue
            and not title_match.widened
            and title_match.candidate_count > 0
            and title_match.target is None
        ):
            return _clarify_completion_target(
                peer,
                attempts=attempts,
                candidates=clarify_candidates,
                offerable=bool(clarify_candidates),
                from_context=from_context,
            )

        # Ids and counts only — the residue tokens and task titles are the
        # user's own words and stay out of the logs.
        log.info(
            "complete_node.resolved_target",
            source=target.source if target else None,
            page_id=target.page_id if target else None,
            active_page_id=active_target.page_id if active_target else None,
            recent_page_id=recent_target.page_id if recent_target else None,
            ledger_page_id=ledger_targets[0].page_id if ledger_targets else None,
            ledger_anchor_count=len(ledger_targets),
            title_page_id=title_match.target.page_id if title_match.target else None,
            candidate_count=title_match.candidate_count,
            candidates_widened=title_match.widened,
            deterministic_answer=title_match.deterministic,
            match_confidence=title_match.confidence,
            residue_token_count=len(residue),
            clarification_attempts=attempts,
            answering_clarification=answering,
        )

        if not target:
            return _clarify_completion_target(
                peer,
                attempts=attempts,
                candidates=clarify_candidates,
                offerable=bool(clarify_candidates),
                from_context=from_context,
            )

        page_id = target.page_id
        display_title = await _resolve_display_title(target, recent_tasks)
        known = ledger_entry(recent_tasks, page_id)
        kind: RecentTaskKind = (
            target.kind if target.kind is not None
            else known["kind"] if known else target.resolved_kind
        )

        if target.needs_notion_write:
            await notion.update_status(page_id=page_id, new_status="Completed")

        if kind == "reminder":
            # A reminder finished before it fired must not fire afterwards.
            await _cancel_pending_reminders(peer, page_id)

        streak = state.get("streak", 0) + 1
        tasks_today = state.get("tasks_completed_today", 0) + 1

        reward_result = await maybe_reward(
            peer=peer,
            # The real title when known; a delivery's sent body is only a
            # fallback so sensitive-task classification still sees the words.
            task_title=display_title or target.task_title,
            notion_page_id=page_id,
            streak=streak,
            work_type=target.work_type,
            energy_required=target.energy_required,
        )

        try:
            # Scoped by page, for every source: a task finished by name or from
            # the ledger may still have a delivered nudge awaiting a reply.
            await reminders.resolve_recent_outbound(
                peer=peer,
                signal_timestamp=target.signal_timestamp or 0,
                notion_page_id=page_id,
            )
        except Exception:
            log.warning(
                "complete_node.recent_outbound_clear_failed",
                page_id=page_id,
                signal_timestamp=target.signal_timestamp,
                exc_info=True,
            )

        reward_draft: OutboundDraft = {
            "recipient": peer,
            "body": _celebration_body(display_title, reward_result["text"]),
            "notion_page_id": page_id,
        }
        if display_title:
            # send_node substitutes the token and guarantees the name appears.
            reward_draft["notion_page_title"] = display_title
        else:
            log.info("complete_node.unnamed_celebration", page_id=page_id, source=target.source)
        # Attach image if one was generated.
        # attachment_path is private; never log the path value.
        if reward_result["attachment_path"]:
            reward_draft["attachment_path"] = reward_result["attachment_path"]

        # The ledger stores the stored title only — never a sent reminder body.
        recent_tasks = record_task_event(
            recent_tasks,
            page_id=page_id,
            title=display_title,
            kind=kind,
            event="completed",
            now=now,
        )

        log.info(
            "complete_node.done",
            page_id=page_id,
            source=target.source,
            streak=streak,
            named=bool(display_title),
        )
        return {
            "pending_outbound": [reward_draft],
            "active_task": None,
            "streak": streak,
            "tasks_completed_today": tasks_today,
            "conversation_state": "idle",
            "pending_clarification": None,
            "recent_tasks": recent_tasks,
        }

    except Exception:
        log.exception("complete_node.error")
        fallback: OutboundDraft = {
            "recipient": peer,
            "body": "Got it, marked done! Nice work.",
            "notion_page_id": None,
        }
        return {
            "pending_outbound": [fallback],
            "active_task": None,
            "conversation_state": "idle",
            "pending_clarification": None,
        }
