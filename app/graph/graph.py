"""LangGraph graph definition for hide-my-list.

Phase A: 2-node graph (classify_intent -> echo) with PostgresSaver checkpointer.
Each peer gets its own conversation thread via thread_id=peer.

Checkpointer lifecycle:
  AsyncPostgresSaver is an async context manager that must be entered before the
  graph is compiled. Use build_postgres_checkpointer() as an async context manager
  in app startup, then pass the entered instance to build_graph(). For tests,
  pass a MemorySaver directly.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
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


@asynccontextmanager
async def build_postgres_checkpointer(
    database_url: str,
) -> AsyncGenerator[Any, None]:
    """Async context manager that sets up and tears down a PostgresSaver.

    Usage:
        async with build_postgres_checkpointer(DATABASE_URL) as cp:
            graph = build_graph(checkpointer=cp)
            ...

    The entered checkpointer is passed to build_graph(); compiling the graph
    with an un-entered AsyncPostgresSaver would leave it without an active
    connection pool.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(database_url) as saver:
        await saver.setup()
        yield saver


def build_graph(checkpointer: Any = None) -> Any:
    """Build and compile the LangGraph state machine.

    Args:
        checkpointer: An already-entered LangGraph checkpointer instance.
            Pass a MemorySaver for unit tests. In production, pass the
            entered saver from build_postgres_checkpointer().
            When None, falls back to MemorySaver (no persistence).

    Returns:
        Compiled LangGraph app.
    """
    if checkpointer is None:
        from langgraph.checkpoint.memory import MemorySaver
        checkpointer = MemorySaver()

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

    return builder.compile(checkpointer=checkpointer)
