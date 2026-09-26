"""Intent classification and routing for the LangGraph pipeline.

Security note: low-confidence classifications default to CHAT rather than
escalating to a potentially wrong intent. This is a prompt-injection mitigation —
if a malicious message tries to force ADD_TASK or COMPLETE via injection, the
classifier errs toward the safer CHAT fallback.

`pending_clarification` is the one thing that overrides that fallback, and it
does not weaken it. The state it reads is written by the agent, not by the
message, so an injected message cannot conjure a clarification to hide behind;
the override is bounded by a TTL and an attempt count; and the node it steers to
runs its own match and its own confidence threshold on any task the message names.
Context sources (`recent_outbound`, `active_task`) resolve the same way they
would on a first-turn completion. A message that reaches complete_node this way
has gained a reader, not a permission.
"""
from __future__ import annotations

import re
from collections.abc import Hashable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.graph.context import render_history, render_recent_tasks
from app.graph.state import Intent, PendingClarification, State

log = structlog.get_logger(__name__)

# How long an unanswered clarification keeps steering the next turn. Long enough
# to cover a user who puts the phone down mid-answer, short enough that a "yeah"
# hours later is read as its own message rather than an answer to a question the
# user has stopped thinking about.
_CLARIFICATION_TTL = timedelta(minutes=30)

# Mirrors complete._MAX_CLARIFICATION_ATTEMPTS. A stored record whose attempts
# field is outside [1, cap] is treated as malformed rather than trusted to steer.
_MAX_CLARIFICATION_ATTEMPTS = 2

# Same user-facing fallback as chat_node's own LLM failure path. Classification
# backend failures use it directly to avoid routing into a second doomed LLM call.
_LLM_UNAVAILABLE_FALLBACK = "Having trouble thinking right now — try again?"

# Intents that can plausibly BE the answer to "which task did you finish?".
# Anything else means the user moved on, and the clarification is dropped rather
# than overriding what they actually asked for.
_CLARIFICATION_ANSWER_INTENTS: frozenset[Intent] = frozenset({"CHAT", "COMPLETE"})

# Replies that point at one of the offered options rather than naming a task.
# While a completion clarification is open these are answers to it, whatever
# the classifier makes of them: a cheap model can read "the first one" as a new
# task and drop the clarification, and the next turn then asks the same
# question again. The match is against the whole normalized message, so "no"
# inside "no it's new, just log it" never matches — only a bare "no" does.
# Positional and affirmative forms only; negatives are in _NEGATIVE_ANSWER_RE
# and handled separately (they decline rather than select).
_OPTION_REFERENCE_RE = re.compile(
    r"(?:the )?(?:first|second|third|1st|2nd|3rd|last|former|latter)(?: one)?"
    r"|(?:number )?[123]"
    r"|that one|this one"
    r"|yes|yep|yeah"
)

# Bare negatives that decline the clarification without selecting any candidate.
# Handled before _OPTION_REFERENCE_RE so routing never steers them to COMPLETE.
_NEGATIVE_ANSWER_RE = re.compile(
    r"no|nope|neither"
    r"|none(?: of (?:them|those))?"
)

# Bare affirmatives. They also match _OPTION_REFERENCE_RE (so they route to
# COMPLETE); complete_node reads them separately to answer a yes/no question.
_AFFIRMATIVE_ANSWER_RE = re.compile(r"yes|yep|yeah")

# Replies that select the only option of a one-option question: an
# affirmative, or a position that can only mean that option.
_SINGLE_OPTION_RE = re.compile(
    r"(?:the )?(?:first|1st|last)(?: one)?"
    r"|(?:number )?1"
    r"|that one|this one"
    r"|yes|yep|yeah"
)

_CLARIFICATION_KINDS: frozenset[str] = frozenset({"complete_target", "unlisted_report"})

# Sent when the user declines the clarification; the task remains open.
_CLARIFICATION_DECLINED_REPLY = "Got it, leaving that open."

# Sent when the user declines the named option of an unlisted-report question
# and the report carries a proposed title: the accomplishment is still offered,
# as a yes/no choice, so the user never has to repeat it.
_OFFER_TO_LOG_AFTER_DECLINE = "Got it. Want me to log '{title}' as done?"


def _normalize_reply(text: str) -> str:
    """Lowercase, strip punctuation, and collapse whitespace."""
    stripped = re.sub(r"[^\w\s]", " ", text.lower())
    return " ".join(stripped.split())


def _is_option_reference(text: str) -> bool:
    return _OPTION_REFERENCE_RE.fullmatch(_normalize_reply(text)) is not None


def _is_negative_answer(text: str) -> bool:
    return _NEGATIVE_ANSWER_RE.fullmatch(_normalize_reply(text)) is not None


def is_affirmative_answer(text: str) -> bool:
    """Whether the whole message is a bare yes ("yes", "yep", "yeah")."""
    return _AFFIRMATIVE_ANSWER_RE.fullmatch(_normalize_reply(text)) is not None


def selects_single_option(text: str) -> bool:
    """Whether the whole message picks the one option a question named."""
    return _SINGLE_OPTION_RE.fullmatch(_normalize_reply(text)) is not None


_INTENT_SYSTEM_PROMPT = """\
You are an intent classifier for a task management assistant called hide-my-list.

Classify the user's message into EXACTLY ONE of these intents:
- ADD_TASK: User wants to add a new task (mentions something they need to do)
- GET_TASK: User wants something to work on (mentions time available, asks what to do)
- COMPLETE: User finished a task — the one they are on, or one they name (says done, finished, completed)
- REJECT: User doesn't want the suggested task (says no, not that one, something else)
- CANNOT_FINISH: User indicates current task is too large or overwhelming
- NEED_HELP: User wants help breaking down or starting their current task
- CHECK_IN: System-initiated follow-up — NEVER classify user messages as this
- CHAT: General conversation or questions — USE THIS when unsure

If the prior conversation shows an in-progress task discussion, treat short
follow-ups (deadlines, clarifications, pronouns like "it") as continuations of
that intent. For example, if the previous turn was the user describing a task
and the current message is "I need to do it by Friday", classify as ADD_TASK.

Rules:
- If unsure or confidence is low, output CHAT (never guess at a wrong intent)
- CHECK_IN is NEVER triggered by user messages — it is system-only
- A past-tense report of something the user did is COMPLETE even when it names
  something not on the list. "I also paid the bill" reports a finished thing;
  it never asks to add one.
- A question about which task was meant ("what task?") is CHAT.
- Accepting a suggestion ("sure", "ok let's do it") is CHAT, not GET_TASK or
  ADD_TASK: the suggested task is already theirs.
- When awaiting clarification is yes, ADD_TASK needs both: the user says the
  thing is new or not on the list, AND asks to log, add, or track it. A reply
  that picks one of the offered options ("the first one", "the second one",
  "that one", "yes") is COMPLETE.
- Respond with ONLY the intent label, nothing else

Examples:
"I need to call the dentist" → ADD_TASK
"I need to renew the car registration this week" → ADD_TASK
"I have 30 minutes" → GET_TASK
"Done!" → COMPLETE
"I also paid the gas bill!" → COMPLETE
"finished that one too" → COMPLETE
"Not that one" → REJECT
"This is too big" → CANNOT_FINISH
"How do I start?" → NEED_HELP
"Hello" → CHAT
"What task?" → CHAT
"sure" (right after the assistant suggested a task) → CHAT
"ok let's do it" (right after the assistant suggested a task) → CHAT
"the first one" (awaiting clarification: yes) → COMPLETE
"the second one" (awaiting clarification: yes) → COMPLETE
"no it's new, just log it" (awaiting clarification: yes) → ADD_TASK
"""


def _live_clarification(state: State) -> PendingClarification | None:
    """Return the outstanding clarification, or None if there is none to honor.

    Fail-closed on anything malformed or expired: a clarification steers the
    next turn away from what the classifier decided, so an unreadable one is
    dropped rather than trusted.
    """
    pending = state.get("pending_clarification")
    if not isinstance(pending, dict):
        return None
    if pending.get("kind") not in _CLARIFICATION_KINDS:
        return None
    if "title" in pending and not isinstance(pending.get("title"), str):
        return None

    raw_asked_at = pending.get("asked_at")
    if not isinstance(raw_asked_at, str) or not raw_asked_at.strip():
        return None
    try:
        asked_at = datetime.fromisoformat(raw_asked_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if asked_at.tzinfo is None:
        asked_at = asked_at.replace(tzinfo=UTC)

    if datetime.now(UTC) - asked_at.astimezone(UTC) > _CLARIFICATION_TTL:
        return None

    raw_attempts = pending.get("attempts")
    if not isinstance(raw_attempts, int) or not (1 <= raw_attempts <= _MAX_CLARIFICATION_ATTEMPTS):
        return None

    raw_candidates = pending.get("candidates")
    if not isinstance(raw_candidates, list):
        return None
    for c in raw_candidates:
        if not (
            isinstance(c, dict)
            and isinstance(c.get("page_id"), str)
            and isinstance(c.get("title"), str)
        ):
            return None

    return pending


def _resolve_with_clarification(
    state: State, classified: Intent
) -> dict[str, Any]:
    """Apply an outstanding clarification to a freshly classified intent.

    Every return path of classify_intent goes through here, so the key is
    written on every turn and a clarification cannot outlive the turn that
    should have consumed it.

    Steering CHAT to COMPLETE does not authorize a completion. complete_node
    still has to match the message against the open-task list and clear its own
    confidence threshold before anything is written; what this changes is which
    node gets to read the answer, not what that node is allowed to do with it.
    """
    pending = _live_clarification(state)
    if pending is None:
        return {
            "intent": classified,
            "pending_clarification": None,
            "classification_error_fallback": False,
        }

    if classified not in _CLARIFICATION_ANSWER_INTENTS:
        log.info(
            "classify_intent.clarification_abandoned",
            has_peer=bool(state.get("peer")),
            intent=classified,
            attempts=pending.get("attempts", 0),
        )
        return {
            "intent": classified,
            "pending_clarification": None,
            "classification_error_fallback": False,
        }

    log.info(
        "classify_intent.clarification_answered",
        has_peer=bool(state.get("peer")),
        classified_intent=classified,
        attempts=pending.get("attempts", 0),
    )
    return {
        "intent": "COMPLETE",
        "pending_clarification": pending,
        "classification_error_fallback": False,
    }


def _resolve_with_backend_fallback(state: State) -> dict[str, Any]:
    """Draft the LLM-unavailable reply without invoking another intent node."""
    peer = state.get("peer", "")
    pending = _live_clarification(state)
    return {
        "intent": "CHAT",
        "pending_clarification": pending,
        "pending_outbound": [
            {
                "recipient": peer,
                "body": _LLM_UNAVAILABLE_FALLBACK,
                "notion_page_id": None,
            }
        ],
        "classification_error_fallback": True,
    }


def _resolve_with_negative_answer(
    state: State, pending: PendingClarification
) -> dict[str, Any]:
    """Handle a bare negative reply to an open completion clarification.

    The user declined the offered option(s). Leave every task open and send a
    brief reply; never write Completed or issue a reward. Routes straight to
    send via classification_error_fallback so chat_node does not run and send a
    second reply.

    One case keeps the conversation going: an unlisted-report question that
    named an option and carries a proposed title. Declining the option does not
    decline the accomplishment, so the reply offers to log the title — a yes/no
    choice rather than a request to repeat it — and stores that as the next
    stage of the same clarification (no candidates, attempts 2). Every other
    negative clears the clarification.
    """
    peer = state.get("peer", "")
    title = str(pending.get("title") or "").strip()
    candidates = pending.get("candidates") or []
    if pending.get("kind") == "unlisted_report" and candidates and title:
        # Booleans only — the title is the user's own words.
        log.info(
            "classify_intent.clarification_offer_to_log",
            has_peer=bool(peer),
        )
        offer: PendingClarification = {
            "kind": "unlisted_report",
            "asked_at": datetime.now(UTC).isoformat(),
            "attempts": _MAX_CLARIFICATION_ATTEMPTS,
            "candidates": [],
            "title": title,
        }
        return {
            "intent": "CHAT",
            "pending_clarification": offer,
            "pending_outbound": [
                {
                    "recipient": peer,
                    "body": _OFFER_TO_LOG_AFTER_DECLINE.format(title=title),
                    "notion_page_id": None,
                }
            ],
            "classification_error_fallback": True,
        }

    log.info(
        "classify_intent.clarification_declined",
        has_peer=bool(peer),
        kind=pending.get("kind"),
    )
    return {
        "intent": "CHAT",
        "pending_clarification": None,
        "pending_outbound": [
            {"recipient": peer, "body": _CLARIFICATION_DECLINED_REPLY, "notion_page_id": None}
        ],
        "classification_error_fallback": True,
    }


async def classify_intent(state: State) -> dict[str, Any]:
    """Classify the incoming message intent using an LLM.

    Uses the cheap tier, which routes to a think=false model configuration
    in app/models.py — classification needs a label, not reasoning. Defaults
    low-confidence to CHAT as a prompt-injection mitigation.
    """
    incoming = state.get("incoming", "").strip()
    if not incoming:
        return _resolve_with_clarification(state, "CHAT")

    # A bare negative reply to an open clarification declines without selecting.
    # Check before the affirmative option-reference guard so "no" never steers
    # to COMPLETE and never reaches complete_node's context fallback.
    live = _live_clarification(state)
    if live is not None and _is_negative_answer(incoming):
        return _resolve_with_negative_answer(state, live)

    # A positional reply to an open clarification ("the first one", "yes") is
    # its answer. Resolve it here, before a model label can drop the
    # clarification complete_node needs to read the option it points at.
    if _live_clarification(state) is not None and _is_option_reference(incoming):
        log.info(
            "classify_intent.clarification_option_reference",
            has_peer=bool(state.get("peer")),
            model_skipped=True,
        )
        return _resolve_with_clarification(state, "COMPLETE")

    try:
        from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage

        from app.models import llm

        model = llm("cheap", caller="classify")

        # Windowed prior turns let short follow-ups resolve against the active
        # discussion (e.g. "by Friday" after an ADD_TASK turn). State.messages
        # is populated by the terminal send node. The recent-task ledger and
        # the conversation state tell the classifier what the last few turns
        # did, which the message text alone does not always say.
        messages_history: list[AnyMessage] = state.get("messages", [])
        prior_context = render_history(messages_history)
        recent_tasks = render_recent_tasks(
            state.get("recent_tasks"), now=datetime.now(UTC)
        )
        awaiting = "yes" if _live_clarification(state) is not None else "no"
        conversation_state = state.get("conversation_state") or "idle"

        messages = [
            SystemMessage(content=_INTENT_SYSTEM_PROMPT),
            HumanMessage(
                content=(
                    f"Prior conversation:\n{prior_context}\n\n"
                    f"Recent tasks:\n{recent_tasks}\n\n"
                    f"Conversation state: {conversation_state}; "
                    f"awaiting clarification: {awaiting}\n\n"
                    f"Current message: {incoming!r}"
                )
            ),
        ]

        response = await model.ainvoke(messages)
        raw = str(response.content).strip().upper()

        # Parse and validate the response
        valid_intents: set[Intent] = {
            "ADD_TASK", "GET_TASK", "COMPLETE", "REJECT",
            "CANNOT_FINISH", "CHECK_IN", "NEED_HELP", "CHAT",
        }

        # Extract the first word that matches a valid intent
        classified: Intent = "CHAT"
        for word in raw.split():
            word_clean = word.strip(".,;:\"'")
            if word_clean in valid_intents:
                classified = word_clean
                break

        # CHECK_IN must never be inferred from user messages
        if classified == "CHECK_IN":
            # Log peer only — no message content (private data discipline)
            log.warning(
                "classify_intent.check_in_from_user_message",
                peer=state.get("peer"),
            )
            classified = "CHAT"

        log.info(
            "classify_intent.classified",
            peer=state.get("peer"),
            intent=classified,
        )
        return _resolve_with_clarification(state, classified)

    except Exception:
        log.exception("classify_intent.error", peer=state.get("peer"))
        # The classifier backend failed before producing an answer. Return the
        # same canned LLM-unavailable draft chat_node would produce, but do not
        # spend a second LLM call trying to reach it.
        return _resolve_with_backend_fallback(state)


def route_intent(state: State) -> str:
    """Return the next node name based on the classified intent.

    Maps each intent to its handler node. Unknown intents route to chat.
    """
    if state.get("classification_error_fallback") is True:
        return "send"

    intent = state.get("intent")

    routing: dict[Intent | None, str] = {
        "ADD_TASK": "intake",
        "GET_TASK": "selection",
        "COMPLETE": "complete",
        "REJECT": "rejection",
        "CANNOT_FINISH": "cannot_finish",
        "CHECK_IN": "check_in",
        "NEED_HELP": "need_help",
        "CHAT": "chat",
        None: "chat",
    }

    return routing.get(intent, "chat")


def build_routing_map() -> dict[Hashable, str]:
    """Return the routing map for conditional edges (all nodes must be declared)."""
    return {
        "intake": "intake",
        "selection": "selection",
        "complete": "complete",
        "rejection": "rejection",
        "cannot_finish": "cannot_finish",
        "check_in": "check_in",
        "need_help": "need_help",
        "chat": "chat",
        "send": "send",
    }


def check_in_route(state: State) -> dict[str, Any]:
    """System-only CHECK_IN injection.

    Called by APScheduler check_in_dispatcher job to inject a CHECK_IN turn.
    Sets intent=CHECK_IN in state so the graph routes to the check_in node.
    Not invokable from user messages (classify_intent guards against this).
    """
    return {"intent": "CHECK_IN"}
