"""Loop 9 — the post-send interaction review recovers a missed completion.

The production shape it guards: the user adds a reminder, says "Done!" a
minute later, and the turn cannot place it — here because the recent-task
ledger is emptied between the turns, the one precondition the fast path
depends on. The fast path asks "which task?". A few seconds later the review
re-reads the whole exchange, sees exactly one open reminder that fits, completes
it, and sends one follow-up naming it.

9b is the yielding half: when the user answers before the review starts, the
review of that turn is cancelled, and the review of the next turn (the user
putting it off) leaves the reminder alone.

Both run through `SignalListener` with the review turned on
(`conversation_with_review`) and settle it with `Conversation.settle_review()`,
which runs the per-turn invariants on the follow-up like on any reply.
"""
from __future__ import annotations

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


async def _add_reminder_then_forget_the_ledger(conversation: Conversation) -> str:
    await conversation.say(
        "remind me to renew my library card tomorrow at 9am",
        expect=Expect(intent="ADD_TASK", sent_count=1),
    )
    # The add turn's own review must leave a correct confirmation alone.
    await conversation.settle_review(expect=Expect(sent_count=0))
    created = conversation.notion.written_pages("create_reminder")
    assert len(created) == 1, f"expected one reminder page, got {sorted(created)}"
    page = next(iter(created))
    # Simulate the ledger missing the add, so the fast path cannot anchor.
    await conversation._write_state({"recent_tasks": []})
    return page


async def test_review_completes_the_reminder_the_turn_could_not_place(
    conversation_with_review: Conversation,
) -> None:
    conversation = conversation_with_review
    page = await _add_reminder_then_forget_the_ledger(conversation)

    await conversation.say(
        "Done!",
        expect=Expect(
            intent="COMPLETE",
            notion_untouched=[page],
            sent_count=1,
            regex_require=[r"(?i)which task"],
        ),
    )
    assert (await conversation.state()).get("pending_clarification")

    settled = await conversation.settle_review(
        expect=Expect(
            notion_status={page: "Completed"},
            db_awaiting_reply=0,
            sent_count=1,
            regex_require=[r"(?i)library"],
        ),
    )

    assert await conversation.outbox_state(page) == ["dead"], (
        "the review completed the reminder but its outbox row is still waiting to fire"
    )
    assert settled.state.get("pending_clarification") is None
    ledger = settled.state.get("recent_tasks") or []
    assert ledger and (ledger[0]["page_id"], ledger[0]["event"]) == (page, "completed")
    # The follow-up joined the history the next turn will read.
    assert settled.sent[0].body in str(settled.state["messages"][-1].content)

    rows = await conversation.review_rows()
    corrections = [row for row in rows if row["verdict"] == "correct"]
    assert [(r["action"], r["action_page_id"], r["executed"], r["follow_up_sent"])
            for r in corrections] == [("complete_task", page, True, True)]


async def test_answering_first_cancels_the_review_and_a_deferral_is_left_alone(
    conversation_with_review: Conversation,
) -> None:
    conversation = conversation_with_review
    page = await _add_reminder_then_forget_the_ledger(conversation)

    await conversation.say(
        "Done!",
        expect=Expect(
            intent="COMPLETE",
            notion_untouched=[page],
            sent_count=1,
            regex_require=[r"(?i)which task"],
        ),
    )
    # Sent inside the review delay: the "Done!" turn's review yields to it.
    deferred = await conversation.say(
        "hold on, let me check which one first",
        expect=Expect(notion_untouched=[page], sent_count=1),
    )
    skipped = [
        entry for entry in deferred.logs
        if entry.get("event") == "interaction_review.skipped"
    ]
    assert [entry.get("reason") for entry in skipped] == ["superseded"]

    await conversation.settle_review(
        expect=Expect(notion_untouched=[page], sent_count=0),
    )
    assert conversation.notion.status_of(page) == "Pending"
    rows = await conversation.review_rows()
    assert not [row for row in rows if row["executed"]]
