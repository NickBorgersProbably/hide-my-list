"""Loop — the add-task confirmation is one glance.

Production confirmations for "I need to <X> this week" read like a report:
work-type label, a time estimate, a numbered plan, and every reminder slot the
deadline planner assigned. The user decision is task + when + first step. The
sub-tasks are still generated and stored; only the reply leaves them out.

This runs through the whole graph so the deadline-series suffix (appended by
intake after scheduling, not by the model) is part of what is checked.
"""
from __future__ import annotations

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


async def test_add_task_with_deadline_confirms_in_one_glance(
    conversation: Conversation,
) -> None:
    added = await conversation.say(
        "I need to book the dentist next week",
        expect=Expect(
            intent="ADD_TASK",
            sent_count=1,
            regex_require=[r"(?i)dentist"],
            regex_forbid=[
                r"\d\)\s",  # numbered plan: "1) ..."
                r"(?i)~?\d+\s?min",  # time estimate
                r"\d+\s+of\s+\d+",  # step count: "1 of 4"
                r"(?i)I'll ping you",  # the whole nudge schedule
            ],
        ),
    )

    created = conversation.notion.written_pages("create_task") | conversation.notion.written_pages(
        "create_reminder"
    )
    assert len(created) == 1, f"expected exactly one page created, got {sorted(created)}"
    page_id = next(iter(created))
    assert "dentist" in conversation.notion.title_of(page_id).lower()

    # At most one scheduled nudge is named, however many were planned.
    assert added.text.count("First nudge") <= 1
