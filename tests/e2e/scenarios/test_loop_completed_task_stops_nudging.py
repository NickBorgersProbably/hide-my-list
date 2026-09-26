"""Loop — a completed task stops getting deadline nudges.

A task with a deadline has a nudge series queued (scheduled through the
production helper, as intake does). One nudge is delivered through the real
worker; the user replies "done". The rest of the series is the cross-turn
handoff: rows written before the conversation, cancelled by the COMPLETE turn,
and read again by a later worker cycle. With every remaining row made due,
that cycle sends nothing, and every undelivered row is dead.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio

_TITLE = "Renew the car registration"


async def test_completed_task_stops_nudging(conversation: Conversation) -> None:
    deadline = datetime.now(UTC) + timedelta(days=4)
    page = conversation.notion.seed_task(
        title=_TITLE,
        work_type="Independent",
        status="Pending",
        due_at_iso=deadline.isoformat(),
    )
    series = await conversation.schedule_deadline_series(
        page_id=page, deadline_at=deadline, title=_TITLE
    )
    assert series, "the scheduler queued no nudges"

    await conversation.deliver_reminder(
        page_id=page,
        body=f"Deadline nudge: {_TITLE}. Want one tiny next step?",
        kind="deadline",
    )
    assert conversation.notion.status_of(page) == "Pending"

    await conversation.say(
        "done",
        expect=Expect(
            intent="COMPLETE",
            notion_status={page: "Completed"},
            db_awaiting_reply=0,
            sent_count=1,
            regex_forbid=[r"(?i)which task"],
        ),
    )

    # Every queued nudge of the series is cancelled by the completion itself,
    # not left for the worker's pre-send check.
    states = await conversation.outbox_state(page)
    assert states.count("delivered") == 1
    assert [s for s in states if s != "delivered"] == ["dead"] * len(series)

    sent = await conversation.run_reminder_worker(make_due=page)
    assert sent == [], "a completed task was nudged"
    assert set(await conversation.outbox_state(page)) == {"delivered", "dead"}
