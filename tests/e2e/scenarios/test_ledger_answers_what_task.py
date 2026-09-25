"""The recent-task ledger answers "what task was that?".

Turn 1 adds a reminder. Turn 2 asks which task that was. Before the ledger,
nothing recorded the page intake just created: the next turn saw only the
windowed message history, and a reply that did not repeat the title gave the
chat node nothing to name. The ledger is written by intake into the
checkpoint and read by the chat prompt on the following turn, so this is a
cross-turn handoff and is covered by a full conversation rather than a
single-node call with a hand-built State.
"""
from __future__ import annotations

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


async def test_what_task_after_adding_a_reminder_names_it(
    conversation: Conversation,
) -> None:
    added = await conversation.say(
        "Remind me to take the bins out at 8pm",
        expect=Expect(intent="ADD_TASK", sent_count=1),
    )

    created = conversation.notion.written_pages("create_reminder") | conversation.notion.written_pages(
        "create_task"
    )
    assert len(created) == 1, f"expected exactly one page created, got {sorted(created)}"
    page_id = next(iter(created))

    ledger = added.state.get("recent_tasks") or []
    assert ledger, "intake created a page but recorded nothing in the recent-task ledger"
    assert ledger[0]["page_id"] == page_id
    assert ledger[0]["event"] == "added"
    assert ledger[0]["title"] == conversation.notion.title_of(page_id)

    writes_before = conversation.notion.mark()
    asked = await conversation.say(
        "What task was that?",
        expect=Expect(intent="CHAT", sent_count=1, regex_require=[r"(?i)bins"]),
    )

    # A question is not an action: nothing is written and the ledger still
    # leads with the task that was added.
    assert conversation.notion.writes[writes_before:] == []
    assert (asked.state.get("recent_tasks") or [{}])[0].get("page_id") == page_id
    assert asked.state.get("turn_actions") == []
