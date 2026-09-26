"""Loop 2 — a deadline nudge is delivered, and "done" completes the task.

The worker delivers deadline nudges without completing the task page (only
reminder pages complete at delivery). The nudge's `recent_outbound` row is the
seam: it must say `deadline`, so the COMPLETE turn knows the page is still open
and writes Completed instead of celebrating a task that stays on the list.
The nudge travels through the real worker for the same reason as every other
delivery in this layer — a fixture insert would pass with the worker's own
`reminder_type` write broken.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


async def test_done_after_a_deadline_nudge_completes_the_task(
    conversation: Conversation,
) -> None:
    page = conversation.notion.seed_task(
        title="Renew the car registration",
        work_type="Independent",
        status="Pending",
        due_at_iso=(datetime.now(UTC) + timedelta(days=2)).isoformat(),
    )
    delivery = await conversation.deliver_reminder(
        page_id=page,
        body="Deadline nudge: Renew the car registration. Want one tiny next step?",
        kind="deadline",
    )
    # Delivery leaves a deadline task open; only a reminder page completes here.
    assert conversation.notion.status_of(page) == "Pending"

    result = await conversation.say(
        "done",
        expect=Expect(
            intent="COMPLETE",
            notion_status={page: "Completed"},
            db_awaiting_reply=0,
            sent_count=1,
            regex_forbid=[r"(?i)which task"],
        ),
    )

    assert result.resolved_page_id == page
    async with conversation.db() as conn:
        cursor = await conn.execute(
            "SELECT reminder_type, awaiting_reply FROM recent_outbound "
            "WHERE peer = %s AND signal_timestamp = %s",
            (conversation.peer, delivery.signal_timestamp),
        )
        row = await cursor.fetchone()
    assert row == ("deadline", False)
    draft = (result.state.get("pending_outbound") or [{}])[0]
    assert draft.get("notion_page_title") == "Renew the car registration"
