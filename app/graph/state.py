"""LangGraph state definition for hide-my-list.

The State TypedDict is the checkpoint unit: one state per (peer, thread_id).
recent_outbound is NOT in State — it lives in the Postgres recent_outbound
table, written by the reminder worker, read by graph nodes at turn start.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

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
    work_type: str
    urgency: int
    time_estimate: int
    energy_required: str


class OutboundDraft(TypedDict):
    """Queued outbound message drained by the terminal send node."""
    recipient: str
    body: str
    notion_page_id: str | None


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

    # Typing for extra keys accepted by LangGraph but not declared above
    __pydantic_extra__: Any
