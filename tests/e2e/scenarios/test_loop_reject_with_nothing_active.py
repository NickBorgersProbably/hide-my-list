"""Loop — a "no" with nothing suggested sends no literal `{task}`.

Nothing has been suggested: no active task, an empty recent-task ledger. A
"nah, not doing that one" classifies REJECT (the reported route; the wording
is one the classifier labels REJECT reliably, unlike "never mind, I'll check
later", which reads as CHAT). Exactly one reply goes out, it carries no
template token, and nothing is written to Notion: the node runs no prompt and
replies with its fixed acknowledgement, so a model cannot write a `{task}` with
no title behind it; `send_node` drops any token-bearing sentence that still
reaches it.
"""
from __future__ import annotations

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


async def test_never_mind_with_nothing_active(conversation: Conversation) -> None:
    page = conversation.notion.seed_task(title="Sort the recycling", work_type="Independent")
    writes_before = conversation.notion.mark()

    result = await conversation.say(
        "nah, not doing that one",
        expect=Expect(
            intent="REJECT",
            sent_count=1,
            notion_untouched=[page],
            regex_forbid=[r"\{task\}", r"\[task\]"],
        ),
    )

    assert conversation.notion.writes[writes_before:] == []
    assert conversation.notion.status_of(page) == "Pending"
    assert result.state.get("active_task") is None
    assert result.state.get("conversation_state") in {"idle", None}
