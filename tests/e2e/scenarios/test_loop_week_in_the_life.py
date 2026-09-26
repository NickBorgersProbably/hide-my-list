"""Loop — five days of one user's list, texted the way people actually text.

Every other loop isolates one seam. This one walks the whole lifecycle in one
checkpoint so the seams have to hold *together*: what intake wrote on day 1 is
what the reminder, the nudge, the title match, and the suggestion read on days
2-5. Days pass through `Conversation.advance_days`, which backdates the ledger,
the checkpoint, and `recent_outbound` together; the clock is never faked.

Day 1  three adds: a task with a deadline, an open-ended task, a reminder.
Day 2  the reminder fires; "done!" resolves it and names it; "whats left?"
       names the two open tasks from the ledger.
Day 3  the deadline nudge fires; "ugh, that got complicated" is a
       cannot-finish, which writes nothing and leaves the nudge answerable.
Day 4  "finally cleaned out the fridge" completes it by title match — the
       nudge and every ledger anchor are a day old by now.
Day 5  "what should I do?" offers the one task left; "done" completes it.

Every turn states which pages it may touch; I3 guarantees no page outside
the created set is ever written.
"""
from __future__ import annotations

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


def _created_since(conversation: Conversation, cursor: int, op: str) -> set[str]:
    return conversation.notion.written_pages(op, since=cursor)


def _top_level(conversation: Conversation, pages: set[str], word: str) -> str:
    """The one page among `pages` whose title contains `word` and has no parent."""
    matches = [
        page
        for page in pages
        if word in conversation.notion.title_of(page).lower()
        and not conversation.notion.pages[page].get("parent_id")
    ]
    assert len(matches) == 1, (
        f"expected one top-level page about {word!r}, got "
        f"{[conversation.notion.title_of(p) for p in pages]}"
    )
    return matches[0]


async def test_a_week_of_adds_reminders_nudges_and_completions(
    conversation: Conversation,
) -> None:
    # -- Day 1: three adds -------------------------------------------------
    cursor = conversation.notion.mark()
    await conversation.say(
        "need to renew my car registration by friday",
        expect=Expect(intent="ADD_TASK", sent_count=1),
    )
    car = _top_level(conversation, _created_since(conversation, cursor, "create_task"), "registration")
    assert conversation.notion.pages[car].get("due_at_iso"), "the deadline was not stored"
    car_title = conversation.notion.title_of(car)

    cursor = conversation.notion.mark()
    await conversation.say(
        "oh and i gotta clean out the fridge at some point",
        expect=Expect(intent="ADD_TASK", notion_untouched=[car], sent_count=1),
    )
    fridge = _top_level(conversation, _created_since(conversation, cursor, "create_task"), "fridge")

    cursor = conversation.notion.mark()
    await conversation.say(
        "remind me to take the recycling out tmrw at 7am",
        expect=Expect(intent="ADD_TASK", notion_untouched=[car, fridge], sent_count=1),
    )
    reminders = _created_since(conversation, cursor, "create_reminder")
    assert len(reminders) == 1, f"expected one reminder page, got {len(reminders)}"
    recycling = next(iter(reminders))
    recycling_title = conversation.notion.title_of(recycling)
    assert await conversation.outbox_state(recycling) == ["pending"]
    for page in (car, fridge, recycling):
        assert conversation.notion.status_of(page) == "Pending"

    # -- Day 2: the reminder fires, "done!", "whats left?" -----------------
    await conversation.advance_days(1)
    await conversation.deliver_reminder(page_id=recycling, body=f"Hey — {recycling_title}")
    # Delivery completes a reminder page; the reply resolves the delivery.
    assert conversation.notion.status_of(recycling) == "Completed"
    assert await conversation.awaiting_reply_count() == 1

    done = await conversation.say(
        "done!",
        expect=Expect(
            intent="COMPLETE",
            notion_untouched=[car, fridge],
            db_awaiting_reply=0,
            sent_count=1,
            regex_forbid=[r"(?i)which task"],
        ),
    )
    assert done.resolved_page_id == recycling
    draft = (done.state.get("pending_outbound") or [{}])[0]
    assert draft.get("notion_page_title") == recycling_title, "the celebration must name it"
    assert "pending" not in await conversation.outbox_state(recycling), (
        "a finished reminder still has an outbox row waiting to fire"
    )

    left = await conversation.say(
        "whats left on my list?",
        expect=Expect(
            intent="CHAT",
            notion_untouched=[car, fridge, recycling],
            sent_count=1,
            regex_require=[r"(?i)fridge", r"(?i)(car|registration)"],
        ),
    )
    assert left.notion_writes_since == conversation.notion.mark(), "CHAT wrote to Notion"

    # -- Day 3: the deadline nudge, "ugh, that got complicated" ------------
    await conversation.advance_days(1)
    await conversation.deliver_reminder(
        page_id=car,
        body=f"Deadline nudge: {car_title} is due Friday. Want one tiny next step?",
        kind="deadline",
    )
    assert conversation.notion.status_of(car) == "Pending", "a nudge never completes its task"

    await conversation.say(
        "ugh that got complicated, cant finish it today. the dmv site wants some form i dont have",
        expect=Expect(
            intent="CANNOT_FINISH",
            notion_untouched=[car, fridge],
            # Not an answer to the nudge: it stays open for a later "done".
            db_awaiting_reply=1,
            sent_count=1,
        ),
    )
    assert conversation.notion.status_of(car) == "Pending"

    # -- Day 4: title match with every context source a day stale ----------
    await conversation.advance_days(1)
    assert await conversation.awaiting_reply_count() == 0, "the day-old nudge should be expired"
    finished = await conversation.say(
        "finally cleaned out the fridge!!",
        expect=Expect(
            intent="COMPLETE",
            notion_status={fridge: "Completed"},
            notion_untouched=[car],
            sent_count=1,
            regex_forbid=[r"(?i)which task"],
        ),
    )
    draft = (finished.state.get("pending_outbound") or [{}])[0]
    assert draft.get("notion_page_id") == fridge

    # -- Day 5: suggestion, then done -------------------------------------
    await conversation.advance_days(1)
    open_before = {
        page for page, fields in conversation.notion.pages.items()
        if fields.get("status") == "Pending" and not fields.get("is_reminder")
    }
    assert car in open_before and fridge not in open_before

    offer = await conversation.say(
        "ok i have like an hour, what should i do",
        expect=Expect(intent="GET_TASK", notion_untouched=[fridge, recycling], sent_count=1),
    )
    offered = (offer.state.get("active_task") or {}).get("page_id")
    assert offered in open_before, "selection offered a page that is not open"
    assert conversation.notion.status_of(offered) == "In Progress"

    await conversation.say(
        "done ✅",
        expect=Expect(
            intent="COMPLETE",
            notion_status={offered: "Completed"},
            notion_untouched=[fridge, recycling],
            db_awaiting_reply=0,
            sent_count=1,
            regex_forbid=[r"(?i)which task"],
        ),
    )

    assert conversation.notion.status_of(recycling) == "Completed"
    assert conversation.notion.status_of(fridge) == "Completed"
    if offered == car:
        assert conversation.notion.status_of(car) == "Completed"
