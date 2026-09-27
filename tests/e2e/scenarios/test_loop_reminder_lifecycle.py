"""Loop — one reminder from "remind me" to "what was that?", and a time change.

Lifecycle (one checkpoint):

1. "remind me to call the pharmacy tomorrow at 6pm" creates a reminder page
   and a pending outbox row. "Tomorrow" keeps that row in the future at any
   hour the suite runs; a past-due intake row would fire in step 2's worker
   cycle first, complete the page, and kill the delivery under test.
2. The reminder fires through the real worker.
3. An unrelated message ("lol my cat...") is not an answer: the delivery
   stays awaiting a reply.
4. "done" resolves that delivery, cancels the page's undelivered rows, and
   names the task.
5. "wait what was that one again" names it from the ledger.
6. A day later a stray "done" completes nothing: every anchor is stale and
   the finished reminder is never celebrated twice.

Time change (its own checkpoint): "actually make it 6pm" after a 5pm
reminder. The test asserts the user-visible outcome: a pending reminder sits
one hour after the first, and the first was neither completed nor lost.
`test_loop_reschedule_reminder.py` pins how: intake moves the same page and
swaps its outbox row rather than creating a second reminder. The time change
is kept out of the lifecycle above so the "done" in step 4 has one anchor.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


async def test_reminder_fired_ignored_done_recalled_and_not_recelebrated(
    conversation: Conversation,
) -> None:
    cursor = conversation.notion.mark()
    await conversation.say(
        "remind me to call the pharmacy tomorrow at 6pm",
        expect=Expect(intent="ADD_TASK", sent_count=1, regex_require=[r"(?i)pharmacy"]),
    )
    created = conversation.notion.written_pages("create_reminder", since=cursor)
    assert len(created) == 1, f"expected one reminder page, got {len(created)}"
    pharmacy = next(iter(created))
    title = conversation.notion.title_of(pharmacy)
    assert await conversation.outbox_state(pharmacy) == ["pending"]

    await conversation.deliver_reminder(page_id=pharmacy, body=f"Hey — {title}")
    assert await conversation.awaiting_reply_count() == 1

    await conversation.say(
        "lol my cat just knocked a plant off the shelf",
        expect=Expect(
            intent="CHAT", notion_untouched=[pharmacy], db_awaiting_reply=1, sent_count=1
        ),
    )

    done = await conversation.say(
        "done",
        expect=Expect(
            intent="COMPLETE",
            db_awaiting_reply=0,
            sent_count=1,
            regex_forbid=[r"(?i)which task"],
        ),
    )
    assert done.resolved_page_id == pharmacy
    assert conversation.notion.status_of(pharmacy) == "Completed"
    assert "pending" not in await conversation.outbox_state(pharmacy), (
        "the intake row for a finished reminder is still waiting to fire"
    )
    draft = (done.state.get("pending_outbound") or [{}])[0]
    assert draft.get("notion_page_title") == title, "the celebration must name the task"

    await conversation.say(
        "wait what was that one again",
        expect=Expect(
            intent="CHAT", notion_untouched=[pharmacy], sent_count=1,
            regex_require=[r"(?i)pharmacy"],
        ),
    )

    await conversation.advance_days(1)
    stray = await conversation.say(
        "done",
        expect=Expect(
            intent="COMPLETE", notion_untouched=[pharmacy], db_awaiting_reply=0, sent_count=1
        ),
    )
    assert stray.resolved_page_id is None
    assert not any(e.get("event") == "complete_node.done" for e in stray.logs), (
        "a day-old 'done' completed something with every anchor stale"
    )


async def test_a_time_change_keeps_a_reminder_at_the_new_time(
    conversation: Conversation,
) -> None:
    cursor = conversation.notion.mark()
    await conversation.say(
        "remind me to call the pharmacy at 5pm",
        expect=Expect(intent="ADD_TASK", sent_count=1),
    )
    created = conversation.notion.written_pages("create_reminder", since=cursor)
    assert len(created) == 1, f"expected one reminder page, got {len(created)}"
    first = next(iter(created))
    first_at = datetime.fromisoformat(str(conversation.notion.pages[first]["remind_at"]))

    await conversation.say(
        "actually make it 6pm",
        expect=Expect(intent="ADD_TASK", sent_count=1, regex_require=[r"(?i)6\s*(pm|:00)"]),
    )
    assert conversation.notion.status_of(first) == "Pending", "a time change completed the reminder"
    at_six = [
        page
        for page, fields in conversation.notion.pages.items()
        if fields.get("is_reminder")
        and fields.get("status") == "Pending"
        and fields.get("remind_at")
        and datetime.fromisoformat(str(fields["remind_at"])) - first_at == timedelta(hours=1)
    ]
    # Intake moves `first` itself; test_loop_reschedule_reminder.py pins that.
    assert len(at_six) == 1, "the follow-up time was not honored: no pending reminder at 6pm"
    assert "pending" in await conversation.outbox_state(at_six[0])
    assert "pharmacy" in conversation.notion.title_of(at_six[0]).lower()
