"""Loop 6 — days later, naming the task still completes it.

The task is added on turn 1, then every fast context source goes stale: the
ledger entry is backdated past its 24h anchor window and any reminder row is
expired. Turn 2 names the task in the user's own words, so title matching —
the one source that never goes stale — has to carry it alone.
"""
from __future__ import annotations

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


async def test_naming_a_task_days_later_completes_it(conversation: Conversation) -> None:
    await conversation.say(
        "add a task: book the dentist appointment",
        expect=Expect(intent="ADD_TASK", sent_count=1),
    )
    # Intake may also file hidden sub-tasks under the page; the parent is the
    # one page created without a parent.
    created = {
        page_id
        for page_id in conversation.notion.written_pages("create_task")
        if not conversation.notion.pages[page_id].get("parent_id")
    }
    assert len(created) == 1, f"expected one top-level task page, got {sorted(created)}"
    page = next(iter(created))

    # Two days pass. Nothing in the checkpoint or Postgres points at the task.
    await conversation.age_recent_tasks(hours=50)
    await conversation.expire_recent_outbound()

    await conversation.say(
        "finally booked the dentist",
        expect=Expect(
            intent="COMPLETE",
            notion_status={page: "Completed"},
            sent_count=1,
            regex_forbid=[r"(?i)which task"],
        ),
    )
