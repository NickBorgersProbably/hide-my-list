"""LangGraph state definition for hide-my-list.

The State TypedDict is the checkpoint unit: one state per (peer, thread_id).
recent_outbound is NOT in State — it lives in the Postgres recent_outbound
table, written by the reminder worker, read by graph nodes at turn start.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, NotRequired

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

Intent = Literal[
    "ADD_TASK",
    "GET_TASK",
    "COMPLETE",
    "REJECT",
    "CANNOT_FINISH",
    "CHECK_IN",
    "NEED_HELP",
    "CHAT",
]

ConversationState = Literal[
    "idle", "intake", "selection", "active", "checking_in"
]


class ActiveTask(TypedDict, total=False):
    """Rehydrated from Notion each turn; Notion is authoritative."""
    page_id: str
    title: str
    status: str
    selected_at: str
    work_type: str
    urgency: int
    time_estimate: int
    energy_required: str
    started_at: str
    check_in_count: int
    rejection_count: int


class OutboundDraft(TypedDict, total=True):
    """Queued outbound message drained by the terminal send node.

    recipient, body, and notion_page_id are always present.
    attachment_path is optional — set only for reward drafts that carry an image.
    The path is private (references the user's task via the manifest table);
    log attachment_count only, never the path itself.
    notion_page_title is optional: set it whenever the body is meant to name the
    task — a suggestion, a confirmation, a completion celebration — and
    send_node guarantees the title appears in the sent text (see
    app/graph/nodes/_task_token.py). Omit it only when no stored title is known.
    The title is private; log booleans only, never the title itself.
    """
    recipient: str
    body: str
    notion_page_id: str | None
    attachment_path: NotRequired[str]
    notion_page_title: NotRequired[str]


class ClarificationCandidate(TypedDict):
    """One task the agent offered, or could have offered, as a clarification answer."""
    page_id: str
    title: str


class PendingClarification(TypedDict, total=False):
    """A question the agent asked and is still waiting on an answer to.

    Without this, a clarifying question leaves no trace: the next turn re-enters
    the same node cold, re-runs the same resolution that already failed, and
    re-sends the same question. `attempts` bounds that loop and `candidates`
    lets the re-ask name concrete options instead of repeating an open question
    (`design/adhd-priorities.md`: offer 2-3 constrained choices).

    `asked_at` is an ISO-8601 UTC timestamp; a stale clarification expires
    rather than binding a much later "yeah" to a question the user has
    forgotten asking.

    `kind` says which question is open. `complete_target` asks which task a
    completion was about. `unlisted_report` follows a report of a finished task
    that matches nothing on the list; `title` is the model's proposed title for
    that accomplishment (grounded in the user's own words, or "" when there is
    none). With a candidate it asks "did you mean <candidate>?"; with a title
    and no candidates it asks whether to log `title` as done. The title is
    private; log booleans only, never the title itself.
    """
    kind: Literal["complete_target", "unlisted_report"]
    asked_at: str
    attempts: int
    candidates: list[ClarificationCandidate]
    title: NotRequired[str]


RecentTaskKind = Literal["task", "reminder"]

RecentTaskEvent = Literal[
    "added", "suggested", "completed", "reminded", "nudged", "rejected"
]


class RecentTaskEntry(TypedDict):
    """One task the conversation touched recently.

    The ledger is the conversation's working memory of "the task we just talked
    about": intake, selection, complete, and rejection record what they did to
    a page, and `hydrate_context` merges reminder deliveries at turn start.

    `title` is the stored Notion title when a node knew it, or "" when the
    entry came from a delivery the ledger had not seen before. It is never a
    sent message body. `at` is an ISO-8601 UTC timestamp. The title is private;
    log ids and counts only.
    """
    page_id: str
    title: str
    kind: RecentTaskKind
    event: RecentTaskEvent
    at: str


TurnActionKind = Literal[
    "notion.create_task",
    "notion.create_reminder",
    "notion.update_status",
    "notion.update_property",
    "reminder.cancel",
    "suggest",
    "reward",
    "clarify",
]


class TurnAction(TypedDict):
    """One thing an intent node did during the current turn.

    The post-send interaction review (`app/graph/interaction_review.py`) reads
    these to judge the turn against what actually happened, not against what
    the reply claims. `page_id` is "" when the action touched no page.
    `status` is the new Notion status for `notion.update_status`, "Completed"
    for a `notion.create_task` that logs finished work, and "" otherwise. Ids and enum values only — never a title or text.
    """
    action: TurnActionKind
    page_id: str
    status: str


class UserPrefs(TypedDict, total=False):
    """User personalization preferences, ported from state.json.user_preferences."""
    timezone: str
    preferred_work_types: list[str]
    default_energy: str
    reward_intensity: str


class State(TypedDict):
    peer: str                              # Signal sender E.164 — thread_id partition
    incoming: str
    intent: Intent | None                  # Literal of the 8 intents
    messages: Annotated[list[AnyMessage], add_messages]
    active_task: ActiveTask | None         # rehydrated from Notion each turn
    streak: int
    tasks_completed_today: int
    user_prefs: UserPrefs                  # ported from state.json
    mood: str | None
    available_minutes: int | None
    conversation_state: ConversationState
    pending_outbound: list[OutboundDraft]  # drained by terminal send node

    # Absent on every checkpoint written before this key existed, so readers use
    # .get() and treat missing as "nothing outstanding". Lifecycle is owned by
    # classify_intent, which is the one node that runs on every turn.
    pending_clarification: NotRequired[PendingClarification | None]

    # Internal routing flag for a classifier transport/backend failure. When
    # true, classify_intent has already produced the user-facing fallback draft,
    # so the graph routes straight to send instead of invoking a second LLM node.
    classification_error_fallback: NotRequired[bool]

    # Recent-task ledger, newest first. Absent on checkpoints written before
    # the key existed, so readers use .get() and treat missing as empty.
    # Writers return the full new list (plain replace, no reducer); the
    # helpers in app/graph/context.py own dedupe, prune, and cap.
    recent_tasks: NotRequired[list[RecentTaskEntry]]

    # What this turn's intent node did, in order. `hydrate_context` resets it
    # to [] at the start of every turn; writers append through
    # `record_turn_action` in app/graph/context.py. Absent on checkpoints
    # written before the key existed, so readers use .get().
    turn_actions: NotRequired[list[TurnAction]]

    # Typing for extra keys accepted by LangGraph but not declared above
    __pydantic_extra__: Any
