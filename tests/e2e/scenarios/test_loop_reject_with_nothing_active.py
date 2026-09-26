"""Loop — "never mind" with nothing suggested sends no literal `{task}`.

Nothing has been suggested: no active task, an empty recent-task ledger. A
"never mind, I'll check later" may classify REJECT (the reported route) or
CHAT; either way exactly one reply goes out, it carries no template token,
and nothing is written to Notion. On the REJECT route the node runs no prompt
and replies with its fixed acknowledgement, so a model cannot write a `{task}`
with no title behind it; `send_node` replaces any token that still reaches it.
"""
from __future__ import annotations

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


async def test_never_mind_with_nothing_active(conversation: Conversation) -> None:
    page = conversation.notion.seed_task(title="Sort the recycling", work_type="Independent")
    writes_before = conversation.notion.mark()

    result = await conversation.say(
        "never mind, I'll check later",
        expect=Expect(
            sent_count=1,
            notion_untouched=[page],
            regex_forbid=[r"\{task\}", r"\[task\]"],
        ),
    )

    assert result.intent in {"REJECT", "CHAT"}, f"unexpected intent {result.intent!r}"
    assert conversation.notion.writes[writes_before:] == []
    assert conversation.notion.status_of(page) == "Pending"
