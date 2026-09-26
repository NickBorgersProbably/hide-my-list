"""Loop 10 — two unresolved asks with ledger options, then the agent stops.

Re-proves the give-up contract with the ledger as the options source: each
turn's reply differs, the first two name the recent tasks, the third leaves
both open without asking again, and the clarification does not survive to
steer the next ordinary message. Tone on every turn is covered by I6.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


def _ago(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


async def test_ledger_options_then_a_shame_safe_give_up(conversation: Conversation) -> None:
    bins = conversation.notion.seed_task(title="Sort the recycling bins", work_type="Physical")
    book = conversation.notion.seed_task(title="Return the library book", work_type="Independent")
    await conversation.seed_recent_tasks(
        {"page_id": bins, "title": "Sort the recycling bins", "kind": "task",
         "event": "added", "at": _ago(3)},
        {"page_id": book, "title": "Return the library book", "kind": "task",
         "event": "added", "at": _ago(10)},
    )

    first = await conversation.say(
        "yep done",
        expect=Expect(
            intent="COMPLETE",
            notion_untouched=[bins, book],
            sent_count=1,
            regex_require=[r"(?i)(sort.*recycling|return.*library|recycling.*bins|library.*book)"],
        ),
    )
    first_pending = first.state.get("pending_clarification") or {}
    assert first_pending.get("attempts") == 1
    assert first_pending.get("candidates"), "the first ask names the recent tasks"

    second = await conversation.say(
        "hmm, can't remember",
        expect=Expect(
            notion_untouched=[bins, book],
            sent_count=1,
            regex_require=[r"(?i)(sort.*recycling|return.*library|recycling.*bins|library.*book)"],
        ),
    )
    assert (second.state.get("pending_clarification") or {}).get("attempts") == 2

    third = await conversation.say(
        "you know the one",
        expect=Expect(notion_untouched=[bins, book], sent_count=1, regex_forbid=[r"(?i)which task"]),
    )
    assert third.state.get("pending_clarification") is None
    assert third.state.get("active_task") is None
    assert third.state.get("conversation_state") == "idle"

    after = await conversation.say("thanks", expect=Expect(notion_untouched=[bins, book]))
    assert after.intent != "COMPLETE", "a cleared clarification must stop steering"
    assert conversation.notion.status_of(bins) == "Pending"
    assert conversation.notion.status_of(book) == "Pending"
