"""Loop — one reminder from "remind me" to "what was that?", with a time change.

1. "remind me to call the pharmacy at 5pm" creates a reminder page and a
   pending outbox row.
2. "actually make it 6pm" is a follow-up to the add. The user's latest time
   is honored: a pending reminder exists one hour after the first. Intake
   has no reschedule path — reminders skip duplicate detection — so the
   follow-up creates a second reminder rather than moving the first. The
   scenario asserts only what is safe either way (the 6pm reminder exists,
   nothing was completed or lost) and does not pin the page count, so a
   future reschedule path passes it unchanged.
3. The 6pm reminder fires through the real worker.
4. An unrelated message ("lol my cat...") is not an answer: the delivery
   stays awaiting a reply.
5. "done" resolves that delivery, cancels the page's undelivered rows, and
   names the task.
6. "wait what was that one again" names it from the ledger.
7. A day later a stray "done" completes nothing: every anchor is stale and
   the finished reminder is never celebrated twice.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


async def test_reminder_moved_fired_ignored_done_and_recalled(
    conversation: Conversation,
) -> None:
    cursor = conversation.notion.mark()
    await conversation.say(
        "remind me to call the pharmacy at 5pm",
        expect=Expect(intent="ADD_TASK", sent_count=1, regex_require=[r"(?i)pharmacy"]),
    )
    created = conversation.notion.written_pages("create_reminder", since=cursor)
    assert len(created) == 1, f"expected one reminder page, got {len(created)}"
    first = next(iter(created))
    first_at = datetime.fromisoformat(str(conversation.notion.pages[first]["remind_at"]))
    assert await conversation.outbox_state(first) == ["pending"]

    await conversation.say(
        "actually make it 6pm",
        expect=Expect(
            intent="ADD_TASK",
            sent_count=1,
            regex_require=[r"(?i)6\s*(pm|:00)"],
        ),
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
    # A reschedule path would move `first` itself; today a second page holds 6pm.
    assert len(at_six) == 1, "the follow-up time was not honored: no pending reminder at 6pm"
    moved = at_six[0]
    assert "pending" in await conversation.outbox_state(moved)
    assert "pharmacy" in conversation.notion.title_of(moved).lower()

    await conversation.deliver_reminder(
        page_id=moved, body=f"Hey — {conversation.notion.title_of(moved)}"
    )
    assert await conversation.awaiting_reply_count() == 1

    await conversation.say(
        "lol my cat just knocked a plant off the shelf",
        expect=Expect(intent="CHAT", notion_untouched=[first, moved], db_awaiting_reply=1, sent_count=1),
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
    assert done.resolved_page_id == moved
    assert conversation.notion.status_of(moved) == "Completed"
    assert "pending" not in await conversation.outbox_state(moved)
    draft = (done.state.get("pending_outbound") or [{}])[0]
    assert "pharmacy" in str(draft.get("notion_page_title") or "").lower()

    await conversation.say(
        "wait what was that one again",
        expect=Expect(intent="CHAT", notion_untouched=[first, moved], sent_count=1, regex_require=[r"(?i)pharmacy"]),
    )

    await conversation.advance_days(1)
    stray = await conversation.say(
        "done",
        expect=Expect(intent="COMPLETE", notion_untouched=[first, moved], db_awaiting_reply=0, sent_count=1),
    )
    assert stray.resolved_page_id is None
    assert not any(e.get("event") == "complete_node.done" for e in stray.logs), (
        "a day-old 'done' completed something with every anchor stale"
    )
