"""Loop — three consecutive rejections escalate to normalization without a task suggestion.

Three REJECT turns in a row with no completion between them reach the third-rejection
path: the model must normalize, offer a mood-or-break choice, and set no alternative
task. This is a cross-turn behavior — each rejection's ledger entry must survive
checkpointing and be visible to the next turn's `_consecutive_rejection_count` call.
A hand-built node state cannot observe that seam; only a real multi-turn conversation
through `SignalListener` can.

Turn 1: GET_TASK — establishes an active task so the first rejection has something
         to decline.
Turn 2: First REJECT — ledger gains a rejected entry + a suggested alternative;
         only the rejected page is written to Notion.
Turn 3: Second REJECT — ledger gains a second rejected entry + another suggested
         alternative; the second rejected page is the only Notion write.
Turn 4: Third REJECT — streak == 3; no alternative task offered, no seeded task
         named, reply normalizes and offers the mood-or-break choice.
"""
from __future__ import annotations

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


async def test_three_consecutive_rejections_escalate_to_normalization(
    conversation: Conversation,
) -> None:
    task_a = conversation.notion.seed_task(
        title="Draft the project brief",
        work_type="Independent",
        energy_required="High",
        urgency=80,
        time_estimate=45,
    )
    task_b = conversation.notion.seed_task(
        title="Reply to the team email",
        work_type="Independent",
        energy_required="Low",
        urgency=60,
        time_estimate=10,
    )
    task_c = conversation.notion.seed_task(
        title="Water the plants",
        work_type="Physical",
        energy_required="Low",
        urgency=40,
        time_estimate=5,
    )
    all_tasks = {task_a, task_b, task_c}

    # Turn 1 — selection offers one task and marks it In Progress.
    offer = await conversation.say(
        "I have about 30 minutes — what should I work on?",
        expect=Expect(
            intent="GET_TASK",
            sent_count=1,
            # A null or unknown selection fails here, inside the turn check, so
            # the debug dump shows which path selection took.
            regex_forbid=[r"(?i)nothing quite fits", r"(?i)couldn't land on one"],
        ),
    )
    first_offered = (offer.state.get("active_task") or {}).get("page_id")
    assert first_offered in all_tasks, f"selection offered no seeded task: {first_offered!r}"
    assert conversation.notion.status_of(first_offered) == "In Progress"

    # Turn 2 — first rejection. The rejected page gets a rejection-count bump;
    # the suggested alternative is not written to.
    notion_cursor_t2 = conversation.notion.mark()
    first_reject = await conversation.say(
        "nah, not feeling that one",
        expect=Expect(intent="REJECT", sent_count=1),
    )
    assert first_reject.state.get("active_task") is None
    first_drafts = first_reject.state.get("pending_outbound") or []
    second_offered = first_drafts[0].get("notion_page_id") if first_drafts else None
    assert second_offered in all_tasks - {first_offered}, (
        f"first rejection offered {second_offered!r}, expected one of the remaining seeded tasks"
    )
    ledger_1 = {
        entry["page_id"]: entry["event"]
        for entry in (first_reject.state.get("recent_tasks") or [])
    }
    assert ledger_1.get(first_offered) == "rejected", (
        "first rejected page should appear as 'rejected' in the ledger after turn 2"
    )
    assert ledger_1.get(second_offered) == "suggested", (
        "second offered page should appear as 'suggested' in the ledger after turn 2"
    )
    t2_up_writes = [
        w for w in conversation.notion.writes[notion_cursor_t2:] if w.op == "update_property"
    ]
    assert len(t2_up_writes) == 1, (
        f"turn 2 (first reject) must write exactly one update_property; got {t2_up_writes}"
    )
    assert t2_up_writes[0].page_id == first_offered
    assert t2_up_writes[0].payload["properties"]["Rejection Count"]["number"] == 1

    # Turn 3 — second rejection. Only the second offered page (now rejected) should
    # be written to Notion; whatever is offered next is untouched.
    notion_cursor_t3 = conversation.notion.mark()
    second_reject = await conversation.say(
        "still not quite right",
        expect=Expect(
            intent="REJECT",
            notion_untouched=list(all_tasks - {second_offered}),
            sent_count=1,
        ),
    )
    assert second_reject.state.get("active_task") is None
    ledger_2 = {
        entry["page_id"]: entry["event"]
        for entry in (second_reject.state.get("recent_tasks") or [])
    }
    assert ledger_2.get(second_offered) == "rejected", (
        "second rejected page should appear as 'rejected' in the ledger after turn 3"
    )
    second_drafts = second_reject.state.get("pending_outbound") or []
    third_offered = second_drafts[0].get("notion_page_id") if second_drafts else None
    assert third_offered in all_tasks - {first_offered, second_offered}, (
        f"second rejection offered {third_offered!r}, expected the last seeded task"
    )
    t3_up_writes = [
        w for w in conversation.notion.writes[notion_cursor_t3:] if w.op == "update_property"
    ]
    assert len(t3_up_writes) == 1, (
        f"turn 3 (second reject) must write exactly one update_property; got {t3_up_writes}"
    )
    assert t3_up_writes[0].page_id == second_offered
    assert t3_up_writes[0].payload["properties"]["Rejection Count"]["number"] == 1

    # Turn 4 — third rejection. The "no" declines the alternative offered in
    # turn 3 (its rejection count is bumped; the other pages are untouched).
    # Streak == 3: no task suggested, no seeded task named, normalization +
    # mood-or-break wording in the reply.
    notion_cursor_t4 = conversation.notion.mark()
    third_reject = await conversation.say(
        "nope, nothing is working for me right now",
        expect=Expect(
            intent="REJECT",
            notion_untouched=list(all_tasks - {third_offered}),
            sent_count=1,
            regex_require=[
                r"(?i)(brain|task mode|not a failure|information|not failing)",
            ],
            regex_forbid=[
                r"(?i)draft the project brief",
                r"(?i)reply to the team email",
                r"(?i)water the plants",
            ],
        ),
    )

    # The draft must carry no alternative page.
    third_drafts = third_reject.state.get("pending_outbound") or []
    assert third_drafts, "third rejection must produce a reply"
    assert third_drafts[0].get("notion_page_id") is None, (
        "third rejection must not suggest an alternative task "
        f"(got page_id={third_drafts[0].get('notion_page_id')!r})"
    )

    # The ledger must record the third rejection too.
    ledger_3 = {
        entry["page_id"]: entry["event"]
        for entry in (third_reject.state.get("recent_tasks") or [])
        if entry.get("event") == "rejected"
    }
    assert set(ledger_3) == all_tasks, (
        f"expected all three seeded pages recorded as rejected after three REJECT turns; got {ledger_3}"
    )
    t4_up_writes = [
        w for w in conversation.notion.writes[notion_cursor_t4:] if w.op == "update_property"
    ]
    assert len(t4_up_writes) == 1, (
        f"turn 4 (third reject) must write exactly one update_property; got {t4_up_writes}"
    )
    assert t4_up_writes[0].page_id == third_offered
    assert t4_up_writes[0].payload["properties"]["Rejection Count"]["number"] == 1
