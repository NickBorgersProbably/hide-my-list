"""LangGraph graph definition for hide-my-list.

Phase A: 2-node graph (classify_intent -> echo) with PostgresSaver checkpointer.
Each peer gets its own conversation thread via thread_id=peer.
"""
from __future__ import annotations

import os
from typing import Any

from langgraph.graph import StateGraph

from app.graph.routing import classify_intent, route_intent
from app.graph.state import State


def _echo_node(state: State) -> dict[str, Any]:
    """Phase A echo node: echoes incoming text back as pending_outbound."""
    peer = state.get("peer", "")
    incoming = state.get("incoming", "")
    return {
        "pending_outbound": [
            {
                "recipient": peer,
                "body": f"[echo] {incoming}",
                "notion_page_id": None,
            }
        ]
    }


def build_graph(checkpointer: Any = None) -> Any:
    """Build and compile the LangGraph state machine.

    Args:
        checkpointer: Optional LangGraph checkpointer. When None, uses
            PostgresSaver connected to DATABASE_URL. Pass a MemorySaver
            in tests that don't need Postgres.

    Returns:
        Compiled LangGraph app.
    """
    builder: StateGraph[State] = StateGraph(State)
    builder.add_node("classify_intent", classify_intent)
    builder.add_node("echo", _echo_node)

    builder.set_entry_point("classify_intent")
    builder.add_conditional_edges(
        "classify_intent",
        route_intent,
        {"echo": "echo"},
    )
    builder.add_edge("echo", "__end__")

    if checkpointer is None:
        database_url = os.environ.get("DATABASE_URL", "")
        if database_url:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            checkpointer = AsyncPostgresSaver.from_conn_string(database_url)
        else:
            # No DB configured (e.g. unit tests without Postgres env)
            from langgraph.checkpoint.memory import MemorySaver
            checkpointer = MemorySaver()

    return builder.compile(checkpointer=checkpointer)
