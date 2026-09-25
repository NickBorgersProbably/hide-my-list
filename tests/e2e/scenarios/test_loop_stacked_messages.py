"""Loop 7 — stacked messages inside the debounce window coalesce.

`SignalListener` debounces same-peer sends (`_InboundMessageBuffer`,
`_process_messages` in `app/ingress/signal_listener.py`): a message that
arrives while the worker is already waiting out
`message_debounce_seconds` for an earlier message from the same peer is
folded into that same turn — one `graph.ainvoke` call over the joined text,
not one call per message. Coalescing is existing production behavior; this
scenario needs a nonzero-debounce conversation to observe it, which is what
`conversation_debounced` (`tests/e2e/conftest.py`) provides.

Splitting "remind me to call mom" and "at 5pm" across two Signal messages a
second apart is exactly the shape a phone keyboard produces when someone
sends a thought and then a correction/addition before the bot replies. If
the two turned into two separate graph calls, the first would have to guess
a time and the second would have nothing to attach "at 5pm" to.
"""
from __future__ import annotations

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


async def test_stacked_messages_coalesce_into_one_add_task(
    conversation_debounced: Conversation,
) -> None:
    conversation = conversation_debounced

    result = await conversation.say_stacked(
        ["remind me to call mom", "at 5pm"],
        expect=Expect(
            intent="ADD_TASK",
            sent_count=1,
            regex_require=[r"(?i)mom", r"(?i)5\s*pm"],
        ),
    )

    created = conversation.notion.written_pages("create_reminder")
    assert len(created) == 1, (
        f"expected exactly one reminder created from the coalesced turn, got "
        f"{len(created)}: {sorted(created)}"
    )

    coalesced_events = [
        entry
        for entry in result.logs
        if str(entry.get("event") or "") == "signal_listener.messages_coalesced"
    ]
    assert coalesced_events, (
        "signal_listener.messages_coalesced was not logged — the two messages "
        "did not go through the debounce/coalescing path"
    )
    assert coalesced_events[0].get("message_count") == 2, (
        f"expected message_count=2 on the coalesced-turn log event, got "
        f"{coalesced_events[0].get('message_count')!r}"
    )
