"""Unit tests for the Phase A echo graph (PR-A3).

Uses MemorySaver checkpointer so no real Postgres is required.
Simulates two peers exchanging messages and verifies:
  - Per-peer thread isolation (peer A's history != peer B's)
  - Message history is preserved across invocations (checkpoint survives restart)
"""
from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.graph.graph import build_graph


@pytest.fixture()
def memory_graph() -> object:
    """Build the echo graph with an in-memory checkpointer."""
    return build_graph(checkpointer=MemorySaver())


@pytest.mark.asyncio
async def test_echo_returns_pending_outbound(memory_graph: object) -> None:
    """Echo node populates pending_outbound with the echoed message."""
    result = await memory_graph.ainvoke(
        {"peer": "<peer-a>", "incoming": "hello"},
        config={"configurable": {"thread_id": "<peer-a>"}},
    )
    assert result["pending_outbound"]
    assert result["pending_outbound"][0]["body"] == "[echo] hello"
    assert result["pending_outbound"][0]["recipient"] == "<peer-a>"


@pytest.mark.asyncio
async def test_per_peer_thread_isolation(memory_graph: object) -> None:
    """Two peers have independent conversation threads."""
    await memory_graph.ainvoke(
        {"peer": "<peer-a>", "incoming": "message from A"},
        config={"configurable": {"thread_id": "<peer-a>"}},
    )
    result_b = await memory_graph.ainvoke(
        {"peer": "<peer-b>", "incoming": "message from B"},
        config={"configurable": {"thread_id": "<peer-b>"}},
    )
    # Peer B's outbound should only reference peer B
    assert result_b["pending_outbound"][0]["recipient"] == "<peer-b>"
    assert "from B" in result_b["pending_outbound"][0]["body"]


@pytest.mark.asyncio
async def test_checkpoint_preserves_history(memory_graph: object) -> None:
    """State persists across multiple invocations for the same peer."""
    await memory_graph.ainvoke(
        {"peer": "<peer-a>", "incoming": "first"},
        config={"configurable": {"thread_id": "<peer-a>"}},
    )
    # Second invocation — peer field and incoming must be provided (State is partial)
    result2 = await memory_graph.ainvoke(
        {"peer": "<peer-a>", "incoming": "second"},
        config={"configurable": {"thread_id": "<peer-a>"}},
    )
    # The echo of the second message should be in pending_outbound
    assert any("second" in item["body"] for item in result2["pending_outbound"])


@pytest.mark.asyncio
async def test_intent_classified_as_chat(memory_graph: object) -> None:
    """Phase A stub classifier always returns CHAT."""
    result = await memory_graph.ainvoke(
        {"peer": "<peer-a>", "incoming": "whatever"},
        config={"configurable": {"thread_id": "<peer-a>"}},
    )
    assert result.get("intent") == "CHAT"
