"""Intent classification and routing for the LangGraph pipeline.

Phase A: stub classify_intent that returns CHAT for all input.
Phase B will replace this with a real LLM-based classifier.
"""
from __future__ import annotations

from app.graph.state import Intent, State


def classify_intent(state: State) -> dict[str, Intent | None]:
    """Classify the incoming message intent.

    Phase A stub: always returns CHAT.
    Phase B replaces this with an LLM call.
    """
    return {"intent": "CHAT"}


def route_intent(state: State) -> str:
    """Return the next node name based on the classified intent."""
    intent = state.get("intent")
    if intent == "CHAT":
        return "echo"
    # All other intents route to echo until Phase B wires real handlers
    return "echo"
