"""Loop 5 — a bare "done" with two recent tasks, answered by position.

Two tasks were added minutes apart and neither is active or reminded, so only
the ledger disambiguates — and it cannot pick one: the newer is not
meaningfully likelier than the older. The agent names both, newest first, and
"the first one" resolves to the option it points at. The offered order is read
back from the checkpoint, because it is what the ordinal refers to.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


def _ago(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


async def test_two_recent_tasks_are_both_named_and_the_first_one_resolves(
    conversation: Conversation,
) -> None:
    package = conversation.notion.seed_task(
        title="Drop off the return package", work_type="Physical"
    )
    recycling = conversation.notion.seed_task(title="Take the recycling out", work_type="Physical")
    await conversation.seed_recent_tasks(
        {"page_id": recycling, "title": "Take the recycling out", "kind": "task",
         "event": "added", "at": _ago(2)},
        {"page_id": package, "title": "Drop off the return package", "kind": "task",
         "event": "added", "at": _ago(5)},
    )

    asked = await conversation.say(
        "done",
        expect=Expect(
            intent="COMPLETE",
            notion_untouched=[package, recycling],
            sent_count=1,
            regex_require=[r"(?i)which task", r"(?i)recycling", r"(?i)package"],
        ),
    )
    options = (asked.state.get("pending_clarification") or {}).get("candidates") or []
    assert [option["page_id"] for option in options] == [recycling, package], (
        "the newest ledger task leads the options"
    )

    await conversation.say(
        "the first one",
        expect=Expect(
            notion_status={recycling: "Completed"},
            notion_untouched=[package],
            sent_count=1,
            regex_forbid=[r"(?i)which task"],
        ),
    )
    assert conversation.notion.status_of(package) == "Pending"
