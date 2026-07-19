"""Unit tests for the eval rig's node-invocation seam.

These run without LLM or Notion access. The rig's own correctness must
not depend on the live services it exists to hold constant — if the
fixture-to-Notion translation drifts from what nodes actually read, every
eval silently scores a degenerate empty-pool response instead of failing.
"""
from __future__ import annotations

import pytest

from app.graph.nodes.rejection import _extract_number, _extract_select, _extract_title
from tests.evals.runner import (
    _as_notion_page,
    _install_notion_stub,
    _invoke_node,
    discover_fixtures,
)


def _fixture(fixture_id: str):
    matches = [f for f in discover_fixtures() if f.id == fixture_id]
    if not matches:
        pytest.fail(f"fixture {fixture_id} not found")
    return matches[0]


def test_as_notion_page_round_trips_through_node_extractors() -> None:
    """The shape the rig emits must be the shape nodes read.

    Asserted against the real extractors in rejection.py rather than a
    hand-copied expectation, so a change to either side fails here.
    """
    page = _as_notion_page(
        {
            "id": "<placeholder-page-id-9>",
            "title": "Sort the recycling bins",
            "work_type": "Physical",
            "time_estimate": 30,
            "urgency": 2,
        }
    )
    props = page["properties"]
    assert page["id"] == "<placeholder-page-id-9>"
    assert _extract_title(props) == "Sort the recycling bins"
    assert _extract_select(props, "Work Type") == "Physical"
    assert _extract_number(props, "Time Estimate (min)", 0) == 30
    assert _extract_number(props, "Urgency", 0) == 2


def test_as_notion_page_omits_unset_properties() -> None:
    """Absent fixture keys must not materialize as empty properties."""
    page = _as_notion_page({"id": "x", "title": "Water the office plants"})
    assert "Work Type" not in page["properties"]
    assert _extract_number(page["properties"], "Time Estimate (min)", 42) == 42


@pytest.mark.asyncio
async def test_notion_stub_serves_fixture_tasks_and_restores() -> None:
    """Reads return the fixture's pool; the original client is restored."""
    from app.tools import notion

    original = notion.query_pending
    fixture = _fixture("rejection-names-alternative-001")

    undo = _install_notion_stub(fixture)
    try:
        result = await notion.query_pending()
        titles = [_extract_title(p["properties"]) for p in result["results"]]
        assert titles == [t["title"] for t in fixture.notion_tasks]
        # Writes are accepted and discarded rather than reaching Notion.
        assert await notion.update_property("<placeholder-page-id-1>", {}) == {}
    finally:
        undo()

    assert notion.query_pending is original


def test_invoke_node_rejects_exception_fallback_output() -> None:
    """A node that falls back must error, not be scored.

    Nodes catch their own exceptions and return a hand-written fallback.
    Those fallbacks are shame-safe by construction, so they satisfy the
    tone contracts without the model having been called — a fixture
    scoring one is green while testing nothing. No LLM proxy is
    configured under unit tests, so the node is forced down that path.
    """
    fixture = _fixture("rejection-names-alternative-001")
    with pytest.raises(RuntimeError, match="exception fallback path"):
        _invoke_node("rejection", fixture)
