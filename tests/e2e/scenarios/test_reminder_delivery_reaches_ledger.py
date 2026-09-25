"""A reminder delivered through the real worker reaches the ledger untitled.

`hydrate_context` merges `recent_outbound` rows into `state["recent_tasks"]` at
the start of every turn, and the merge deliberately writes an empty title
unless the ledger already knows one — the ledger never copies a sent message
body. This is the listener-facing half of that contract: the reminder travels
through the real `reminder_worker` (the table's only writer), then one live
CHAT turn observes the ledger entry without answering or resolving anything.

A hand-built State dict cannot see this seam: `deliver_reminder` writes
`recent_outbound` outside the graph entirely, and only a live turn through
`hydrate_context` merges it into the checkpoint.
"""
from __future__ import annotations

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


async def test_reminder_delivery_reaches_ledger_as_reminded(
    conversation: Conversation,
) -> None:
    page = conversation.notion.seed_task(
        title="Water the plants",
        work_type="Independent",
        is_reminder=True,
        reminder_status="pending",
    )
    conversation.offered.add(page)

    await conversation.deliver_reminder(page_id=page, body="Hey — water the plants")
    assert await conversation.awaiting_reply_count() == 1

    result = await conversation.say(
        "what was that reminder about?",
        expect=Expect(
            intent="CHAT",
            sent_count=1,
            notion_untouched=[page],
            db_awaiting_reply=1,
        ),
    )

    ledger = result.state.get("recent_tasks") or []
    entry = next((row for row in ledger if row.get("page_id") == page), None)
    assert entry is not None, f"reminder delivery for {page} never reached the ledger"
    assert entry["event"] == "reminded"
    assert entry["kind"] == "reminder"
    assert entry["title"] == "", (
        "the ledger must not copy the sent reminder body into the title"
    )

    # The CHAT turn only answered a question; it must not resolve the reminder
    # the peer has not yet replied to.
    assert await conversation.awaiting_reply_count_for_page(page) == 1
