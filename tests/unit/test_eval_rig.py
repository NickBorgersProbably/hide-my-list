"""Unit tests for the eval rig's node-invocation seam.

These run without LLM or Notion access. The rig's own correctness must
not depend on the live services it exists to hold constant — if the
fixture-to-Notion translation drifts from what nodes actually read, every
eval silently scores a degenerate empty-pool response instead of failing.
"""
from __future__ import annotations

import pytest

from app.graph.nodes._task_match import extract_checkbox
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
            "status": "In Progress",
            "is_reminder": False,
        }
    )
    props = page["properties"]
    assert page["id"] == "<placeholder-page-id-9>"
    assert _extract_title(props) == "Sort the recycling bins"
    assert _extract_select(props, "Work Type") == "Physical"
    assert _extract_select(props, "Status") == "In Progress"
    assert extract_checkbox(props, "Is Reminder") is False
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


def test_evaluate_contracts_scores_regex_raw_and_judge_delivered(monkeypatch) -> None:
    """regex_* score the raw draft body; judge/shame_safe score the delivered body.

    The token invariant ("the model wrote {task}") is only assertable on the
    raw body, while a judge reading the raw body dings the token as an
    unfilled placeholder — each surface exists for the contract kinds that
    need it.
    """
    from tests.evals import judge as judge_mod
    from tests.evals.runner import Contract, evaluate_contracts

    judged_surfaces: list[str] = []

    def _fake_score(*, rubric: str, response: str, **_kwargs):
        judged_surfaces.append(response)
        return judge_mod.JudgeResult(score=1.0, reasoning="ok", from_cache=False)

    monkeypatch.setattr(judge_mod, "score", _fake_score)

    raw = "How's {task} going?"
    delivered = "How's Sort the mail pile going?"
    contracts = [
        Contract(kind="regex_require", spec={"pattern": r"\{task\}"}),
        Contract(kind="judge", spec={"rubric": "names the task", "threshold": 0.7}),
        Contract(kind="shame_safe", spec={"threshold": 0.8}),
    ]
    results = evaluate_contracts(contracts, raw, delivered)

    assert [r.passed for r in results] == [True, True, True]
    assert judged_surfaces == [delivered, delivered]


def test_invoke_node_injects_fresh_selected_at_for_complete() -> None:
    """A fixture active_task without selected_at must still be active.

    complete_node treats a missing selected_at as "no active task" (24h
    TTL) and asks which task was meant — a static fixture can never carry
    a fresh timestamp, so the runner injects run-time now. complete_node
    makes no LLM call, so this runs end-to-end in the unit environment.
    """
    fixture = _fixture("complete-done-001")
    body, _title = _invoke_node("complete", fixture)
    assert body
    assert "which task did you mean" not in body.lower()


def test_invoke_node_scores_the_validated_interaction_review_verdict(monkeypatch) -> None:
    """The review branch scores what production would act on.

    `judge_turn` is stubbed (no model under unit tests); the runner must feed
    it the fixture's turn and open tasks, validate the text with the real
    `parse_verdict` against those ids, and return the re-serialized verdict.
    """
    import json

    from app.graph import interaction_review

    fixture = _fixture("interaction-review-recovers-completion-001")
    seen: list[object] = []

    async def fake_judge_turn(inputs):
        seen.append(inputs)
        return json.dumps({
            "verdict": "correct",
            "reason": "placeholder",
            "action": "complete_task",
            "page_id": "<placeholder-page-id-1>",
            "title": None,
            "due": None,
            "follow_up_message": "{task} — marked that one done.",
        })

    monkeypatch.setattr(interaction_review, "judge_turn", fake_judge_turn)
    body, title = _invoke_node("interaction_review", fixture)

    assert title is None
    verdict = json.loads(body)
    assert (verdict["verdict"], verdict["action"], verdict["page_id"]) == (
        "correct", "complete_task", "<placeholder-page-id-1>",
    )
    (inputs,) = seen
    assert inputs.user_message == "Done!"
    assert inputs.intent == "COMPLETE"
    assert inputs.reply == "I can mark that done. Which task did you mean?"
    assert [task["id"] for task in inputs.open_tasks] == ["<placeholder-page-id-1>"]
    assert inputs.open_tasks[0]["kind"] == "reminder"
    assert "- clarify" in inputs.turn_actions


def test_invoke_node_marks_an_invalid_review_verdict(monkeypatch) -> None:
    """A verdict production would discard must fail the fixture's json_schema."""
    from app.graph import interaction_review
    from tests.evals.runner import Contract, evaluate_contracts

    fixture = _fixture("interaction-review-recovers-completion-001")

    async def fake_judge_turn(_inputs):
        # An id the model invented: parse_verdict refuses it.
        return (
            '{"verdict": "correct", "reason": "x", "action": "complete_task", '
            '"page_id": "<invented>", "title": null, "due": null, '
            '"follow_up_message": "{task} done."}'
        )

    monkeypatch.setattr(interaction_review, "judge_turn", fake_judge_turn)
    body, _title = _invoke_node("interaction_review", fixture)

    assert body.startswith("INVALID_VERDICT:")
    (result,) = evaluate_contracts([Contract(kind="json_schema", spec={})], body)
    assert result.passed is False
