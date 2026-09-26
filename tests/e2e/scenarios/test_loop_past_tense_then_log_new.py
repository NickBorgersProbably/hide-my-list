"""Loop — a past-tense report of something never added, then "it's new".

1. "I also paid the gas bill!" is a report of something done. It classifies
   COMPLETE even though nothing on the list matches; production routed this
   shape to ADD_TASK. Neither open task is touched. The reply asks whether to
   add it — its wording is not the contract here.
2. "no it's new, just log it" answers that question. It classifies ADD_TASK,
   which drops the open clarification, and intake takes the task from the
   earlier message instead of asking again. The thing is already done, so the
   page is created Completed and the reply celebrates it by name — an
   accomplishment is never recorded as another open obligation.

The handoff is cross-turn: turn 2's intake reads turn 1's user message from the
checkpointed history, so only a conversation can show it.
"""
from __future__ import annotations

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


async def test_past_tense_report_then_log_it_as_new(conversation: Conversation) -> None:
    dentist = conversation.notion.seed_task(title="Book the dentist", work_type="Independent")
    landlord = conversation.notion.seed_task(
        title="Email the landlord about the lease", work_type="Independent"
    )

    await conversation.say(
        "I also paid the gas bill!",
        expect=Expect(
            intent="COMPLETE",
            notion_untouched=[dentist, landlord],
            sent_count=1,
        ),
    )
    assert conversation.notion.written_pages("create_task") == set()

    notion_cursor = conversation.notion.mark()
    logged = await conversation.say(
        "no it's new, just log it",
        expect=Expect(
            intent="ADD_TASK",
            notion_untouched=[dentist, landlord],
            sent_count=1,
            regex_require=[r"(?i)gas"],
        ),
    )

    new_tasks = conversation.notion.written_pages("create_task", since=notion_cursor)
    new_reminders = conversation.notion.written_pages("create_reminder", since=notion_cursor)
    new_creates = new_tasks | new_reminders
    assert len(new_creates) == 1, (
        f"expected exactly one create write after turn 2, got {len(new_creates)}: {new_creates}"
    )
    created_id = next(iter(new_creates))
    title = conversation.notion.title_of(created_id).lower()
    assert "gas bill" in title, f"expected a page about the gas bill, got {title!r}"
    status = conversation.notion.status_of(created_id)
    assert status == "Completed", f"a logged finished item must be Completed, got {status!r}"
    assert logged.state.get("pending_clarification") is None
