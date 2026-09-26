"""Loop — two bad days: every friction point a low-energy user hits, in order.

The shame-risk moments docs/ai-prompts/rejection.md and design/adhd-priorities.md
single out — repeated no's, "I did nothing", a question the user does not want
to answer — all land in one checkpoint here. I6 (no banned shame phrase) runs
on every turn, so the tone contract is enforced on each of them; the scenario
asserts what the system *did*.

Day 1
1. "what should i do" offers a task and marks it In Progress.
2. "nah not that one" declines it; another task is offered by name.
3. "no" declines the alternative; neither declined task is offered again.
4. "nope none of those either" — the third no — is normalized, and no
   declined task is offered again (rejection.md, Escalation After Multiple
   Rejections).
5. "i did nothing today lol" is CHAT: warm, and nothing is written.

Day 2
6. "done!" with every anchor a day stale completes nothing and asks.
7. "nope" declines the question deterministically — no model call, the fixed
   "Got it, leaving that open." reply, and the clarification is cleared.
8. "i took the dog to the vet this morning tho" reports something that is on
   no list; the reply offers to log it as a yes/no question.
9. "yes" logs it Completed and celebrates it by name.
"""
from __future__ import annotations

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


def _events(result: object) -> set[str]:
    return {str(entry.get("event") or "") for entry in getattr(result, "logs", [])}


def _llm_calls(result: object) -> int:
    return sum(
        1 for entry in getattr(result, "logs", []) if entry.get("event") == "llm.call.end"
    )


async def test_rejections_a_nothing_day_a_declined_question_and_an_unlisted_win(
    conversation: Conversation,
) -> None:
    notion = conversation.notion
    laundry = notion.seed_task(
        title="Fold the laundry", work_type="Physical", energy_required="Low",
        urgency=60, time_estimate=20,
    )
    water = notion.seed_task(
        title="Pay the water bill", work_type="Independent", energy_required="Low",
        urgency=70, time_estimate=10,
    )
    mail = notion.seed_task(
        title="Sort the mail pile", work_type="Independent", energy_required="Medium",
        urgency=40, time_estimate=25,
    )
    seeded = {laundry, water, mail}

    # -- Day 1 -------------------------------------------------------------
    offer = await conversation.say(
        "i have maybe 20 min. what should i do",
        expect=Expect(intent="GET_TASK", sent_count=1),
    )
    first = (offer.state.get("active_task") or {}).get("page_id")
    assert first in seeded, f"selection offered no seeded task: {first!r}"
    assert notion.status_of(first) == "In Progress"

    declined = await conversation.say(
        "nah not that one",
        expect=Expect(intent="REJECT", notion_untouched=sorted(seeded - {first}), sent_count=1),
    )
    assert declined.state.get("active_task") is None
    drafts = declined.state.get("pending_outbound") or []
    second = drafts[0].get("notion_page_id") if drafts else None
    assert second in seeded - {first}, "the first decline must offer a different seeded task"
    ledger = {e["page_id"]: e["event"] for e in declined.state.get("recent_tasks") or []}
    assert ledger.get(first) == "rejected" and ledger.get(second) == "suggested"

    again = await conversation.say(
        "no",
        expect=Expect(intent="REJECT", notion_untouched=sorted(seeded), sent_count=1),
    )
    drafts = again.state.get("pending_outbound") or []
    third = drafts[0].get("notion_page_id") if drafts else None
    assert third not in {first, second}, "a task the user just turned down was offered again"

    third_no = await conversation.say(
        "nope none of those either",
        expect=Expect(
            intent="REJECT",
            notion_untouched=sorted(seeded),
            sent_count=1,
            regex_require=[
                r"(?i)(not a failure|task mode|break|rest|later|no pressure|here when|whenever)"
            ],
        ),
    )
    # Every open task has now been turned down (the first stays In Progress,
    # so it is not even a candidate): any task offered here is a re-offer.
    drafts = third_no.state.get("pending_outbound") or []
    assert not (drafts and drafts[0].get("notion_page_id")), (
        "the third no was answered by re-offering a task the user already declined"
    )

    nothing = await conversation.say(
        "i did nothing today lol",
        expect=Expect(
            intent="CHAT",
            notion_untouched=sorted(seeded),
            sent_count=1,
            regex_forbid=[r"(?i)you (should|need to|have to)", r"(?i)why (didn't|did not)"],
        ),
    )
    assert nothing.notion_writes_since == notion.mark(), "a CHAT turn wrote to Notion"

    # -- Day 2 -------------------------------------------------------------
    await conversation.advance_days(1)
    asked = await conversation.say(
        "done!",
        expect=Expect(intent="COMPLETE", notion_untouched=sorted(seeded), sent_count=1),
    )
    assert "complete_node.done" not in _events(asked), "a stale 'done' completed a task"
    assert (asked.state.get("pending_clarification") or {}).get("kind") == "complete_target"

    left_open = await conversation.say(
        "nope",
        expect=Expect(
            intent="CHAT",
            notion_untouched=sorted(seeded),
            sent_count=1,
            regex_require=[r"^Got it, leaving that open\.$"],
        ),
    )
    assert "classify_intent.clarification_declined" in _events(left_open)
    assert _llm_calls(left_open) == 0, "a bare 'nope' must decline without a model call"
    assert left_open.state.get("pending_clarification") is None

    cursor = notion.mark()
    report = await conversation.say(
        "i took the dog to the vet this morning tho",
        expect=Expect(
            intent="COMPLETE",
            notion_untouched=sorted(seeded),
            sent_count=1,
            regex_require=[r"(?i)want me to log '[^']*vet[^']*' as done\?"],
        ),
    )
    pending = report.state.get("pending_clarification") or {}
    assert pending.get("kind") == "unlisted_report"
    assert notion.written_pages("create_task", since=cursor) == set()

    logged = await conversation.say(
        "yes",
        expect=Expect(
            intent="COMPLETE",
            notion_untouched=sorted(seeded),
            sent_count=1,
            regex_require=[r"(?i)vet"],
        ),
    )
    assert "classify_intent.clarification_option_reference" in _events(logged)
    created = notion.written_pages("create_task", since=cursor)
    assert len(created) == 1, f"expected one logged page, got {len(created)}"
    vet = next(iter(created))
    assert "vet" in notion.title_of(vet).lower()
    assert notion.status_of(vet) == "Completed", "a logged win is recorded Completed"
    assert logged.state.get("pending_clarification") is None
    for page in seeded - {first}:
        assert notion.status_of(page) == "Pending"
