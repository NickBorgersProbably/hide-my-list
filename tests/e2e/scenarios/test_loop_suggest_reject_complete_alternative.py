"""Loop — a suggestion accepted, then turned down, and the alternative finished.

Four turns, the shape of a real session:

1. "what should I do?" — selection offers a task and marks it In Progress.
2. "sure" — accepting is CHAT, not a new request. Only the routing is asserted.
3. "that got complicated, something else?" — rejection clears the active task,
   offers the other task by name, and records the decline and the offer in the
   recent-task ledger. The alternative is not written to.
4. The user reports the alternative done by name — title match completes it
   and leaves the rejected task alone.

Turn 2 is the classifier seam: "sure" used to have no example, so it could
read as GET_TASK (a second suggestion) or ADD_TASK. Turns 3-4 are the
cross-turn handoff from rejection's ledger/draft to a later COMPLETE, which only
a conversation can observe.
"""
from __future__ import annotations

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio

# Each seeded task with the words a reply naming it must contain, and the
# message the user sends when reporting it done.
_GARAGE = ("Clean out the garage", r"(?i)garage", "ok, cleaned out the garage")
_SCHOOL = ("Reply to the school email", r"(?i)school", "ok, replied to the school email")


async def test_suggest_accept_reject_then_complete_the_alternative(
    conversation: Conversation,
) -> None:
    garage = conversation.notion.seed_task(
        title=_GARAGE[0],
        work_type="Physical",
        energy_required="High",
        urgency=90,
        time_estimate=90,
    )
    school = conversation.notion.seed_task(
        title=_SCHOOL[0],
        work_type="Independent",
        energy_required="Low",
        urgency=60,
        time_estimate=10,
    )
    by_page = {garage: _GARAGE, school: _SCHOOL}

    offer = await conversation.say(
        "I've got a couple of hours and plenty of energy — what should I do?",
        expect=Expect(intent="GET_TASK", sent_count=1),
    )
    first = (offer.state.get("active_task") or {}).get("page_id")
    assert first in by_page, f"selection offered no seeded task: {first!r}"
    alt = school if first == garage else garage
    assert conversation.notion.status_of(first) == "In Progress"

    # Accepting a suggestion classifies CHAT. Routing only: the reply wording
    # is not the contract here.
    accepted = await conversation.say(
        "sure",
        expect=Expect(intent="CHAT", sent_count=1),
    )
    assert (accepted.state.get("active_task") or {}).get("page_id") == first

    rejected = await conversation.say(
        "that got complicated, something else?",
        expect=Expect(
            intent="REJECT",
            # Offering the alternative writes nothing to it; the rejected page
            # only gets its rejection count bumped.
            notion_untouched=[alt],
            sent_count=1,
            regex_require=[by_page[alt][1]],
        ),
    )
    assert rejected.state.get("active_task") is None
    drafts = rejected.state.get("pending_outbound") or []
    assert drafts and drafts[0].get("notion_page_id") == alt
    assert drafts[0].get("notion_page_title") == by_page[alt][0]
    ledger = {
        entry["page_id"]: entry["event"] for entry in rejected.state.get("recent_tasks") or []
    }
    assert ledger.get(first) == "rejected"
    assert ledger.get(alt) == "suggested"

    await conversation.say(
        by_page[alt][2],
        expect=Expect(
            intent="COMPLETE",
            notion_status={alt: "Completed"},
            notion_untouched=[first],
            sent_count=1,
            regex_forbid=[r"(?i)which task"],
        ),
    )
