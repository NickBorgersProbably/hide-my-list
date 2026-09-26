"""Loop 1 — a reminder is added, "Done!" a minute later, then "what task was that?".

The production shape: the user asks for a reminder and finishes the thing
before it fires. The reminder page is Pending and its outbox row is still
waiting, so neither `recent_outbound` nor `active_task` knows about it — only
the recent-task ledger intake wrote on turn 1 does. Three turns, because each
seam is a different handoff: intake → ledger, ledger → COMPLETE (write,
outbox cancel, named celebration), COMPLETE → ledger → CHAT.
"""
from __future__ import annotations

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


async def test_done_right_after_adding_a_reminder_completes_it(
    conversation: Conversation,
) -> None:
    await conversation.say(
        "remind me to take the bins out at 8pm",
        expect=Expect(intent="ADD_TASK", sent_count=1),
    )
    created = conversation.notion.written_pages("create_reminder")
    assert len(created) == 1, f"expected one reminder page, got {sorted(created)}"
    page = next(iter(created))
    title = conversation.notion.title_of(page)
    assert await conversation.outbox_state(page) == ["pending"]

    done = await conversation.say(
        "Done!",
        expect=Expect(
            intent="COMPLETE",
            notion_status={page: "Completed"},
            db_awaiting_reply=0,
            sent_count=1,
            regex_forbid=[r"(?i)which task"],
        ),
    )

    assert await conversation.outbox_state(page) == ["dead"], (
        "the reminder was marked done but its outbox row is still waiting to fire"
    )
    assert done.state.get("active_task") is None
    draft = (done.state.get("pending_outbound") or [{}])[0]
    assert draft.get("notion_page_title") == title, "the celebration must name the task"
    ledger = done.state.get("recent_tasks") or []
    assert ledger and ledger[0]["page_id"] == page and ledger[0]["event"] == "completed"

    await conversation.say(
        "what task was that?",
        expect=Expect(
            intent="CHAT",
            notion_untouched=[page],
            sent_count=1,
            regex_require=[r"(?i)bins"],
        ),
    )
