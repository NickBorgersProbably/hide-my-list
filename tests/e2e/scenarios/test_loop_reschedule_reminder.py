"""Loop — move a reminder, then "I can't finish that" after a deadline nudge.

One checkpoint:

1. "remind me to call the pharmacy tomorrow at 5pm" creates one reminder page
   and one pending outbox row. Tomorrow keeps both times in the future at any
   hour the suite runs, so the worker never delivers them mid-scenario.
2. "actually make it 6pm" moves that page: no second reminder page, the 5pm
   row is dead (`rescheduled by user`), one pending row waits one hour later,
   and the page's `Remind At` says the same.
3. A deadline nudge for an open task goes out through the real worker.
4. "I can't finish that today, ..." is about the nudged task: the reply names
   it, the task stays open, and the remaining work lands in Notion as hidden
   sub-tasks under that page.

Step 2 needs the ledger entry intake wrote in step 1; step 4 needs the
`nudged` entry `hydrate_context` merges from the delivery. Both anchors cross
a turn boundary, which is what this layer exists to observe.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


async def _outbox_rows(conversation: Conversation, page_id: str) -> list[tuple[str, str | None, datetime]]:
    async with conversation.db() as conn:
        cursor = await conn.execute(
            """
            SELECT state, last_error, due_at FROM reminder_outbox
             WHERE peer = %s AND notion_page_id = %s
             ORDER BY created_at ASC, due_at ASC
            """,
            (conversation.peer, page_id),
        )
        return [(str(row[0]), row[1], row[2]) for row in await cursor.fetchall()]


async def test_a_time_change_moves_the_reminder_and_cannot_finish_follows_the_nudge(
    conversation: Conversation,
) -> None:
    # -- 1. set a reminder ---------------------------------------------------
    cursor = conversation.notion.mark()
    await conversation.say(
        "remind me to call the pharmacy tomorrow at 5pm",
        expect=Expect(intent="ADD_TASK", sent_count=1, regex_require=[r"(?i)pharmacy"]),
    )
    created = conversation.notion.written_pages("create_reminder", since=cursor)
    assert len(created) == 1, f"expected one reminder page, got {len(created)}"
    pharmacy = next(iter(created))
    first_at = datetime.fromisoformat(str(conversation.notion.pages[pharmacy]["remind_at"]))
    assert await conversation.outbox_state(pharmacy) == ["pending"]

    # -- 2. move it ----------------------------------------------------------
    cursor = conversation.notion.mark()
    await conversation.say(
        "actually make it 6pm",
        expect=Expect(intent="ADD_TASK", sent_count=1, regex_require=[r"(?i)6\s*(pm|:00)"]),
    )
    assert not conversation.notion.written_pages("create_reminder", since=cursor), (
        "the time change created a second reminder page"
    )
    assert conversation.notion.written_pages("update_property", since=cursor) == {pharmacy}
    reminder_pages = [
        page for page, fields in conversation.notion.pages.items() if fields.get("is_reminder")
    ]
    assert reminder_pages == [pharmacy]
    moved_at = datetime.fromisoformat(str(conversation.notion.pages[pharmacy]["remind_at"]))
    assert moved_at - first_at == timedelta(hours=1), "the page does not hold the new time"
    assert conversation.notion.status_of(pharmacy) == "Pending"

    rows = await _outbox_rows(conversation, pharmacy)
    assert [(state, error) for state, error, _ in rows] == [
        ("dead", "rescheduled by user"),
        ("pending", None),
    ], rows
    assert rows[0][2] == first_at.astimezone(UTC)
    assert rows[1][2] == moved_at.astimezone(UTC), "the new outbox row is not at the new time"

    # -- 3. a deadline nudge for an open task --------------------------------
    car = conversation.notion.seed_task(
        title="Renew the car registration",
        work_type="Independent",
        status="Pending",
        time_estimate=45,
        due_at_iso=(datetime.now(UTC) + timedelta(days=2)).isoformat(),
    )
    await conversation.deliver_reminder(
        page_id=car,
        body="Deadline nudge: Renew the car registration is due Tuesday. Want one tiny next step?",
        kind="deadline",
    )
    assert conversation.notion.status_of(car) == "Pending"

    # -- 4. "I can't finish that today" --------------------------------------
    cursor = conversation.notion.mark()
    result = await conversation.say(
        "I can't finish that today. I found the renewal form online but still "
        "need to fill it in and pay the fee",
        expect=Expect(
            intent="CANNOT_FINISH",
            notion_untouched=[car, pharmacy],
            # Not an answer to the nudge: it stays open for a later "done".
            db_awaiting_reply=1,
            sent_count=1,
            regex_require=[r"(?i)(car|registration)"],
            regex_forbid=[r"(?i)(which|what) task"],
        ),
    )
    draft = (result.state.get("pending_outbound") or [{}])[0]
    assert draft.get("notion_page_id") == car
    assert draft.get("notion_page_title") == "Renew the car registration"
    assert conversation.notion.status_of(car) == "Pending"

    children = [
        conversation.notion.pages[page]
        for page in conversation.notion.written_pages("create_task", since=cursor)
    ]
    assert children, "the remaining work was not written to Notion"
    assert all(child.get("parent_id") == car for child in children), (
        "a cannot-finish sub-task landed outside the nudged task"
    )
