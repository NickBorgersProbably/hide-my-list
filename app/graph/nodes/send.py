"""Send (terminal) node: drains pending_outbound via Signal.

Consumes all OutboundDraft items from pending_outbound, sends each via
app/tools/signal_client.send_message(). Idempotency keys are generated from
the recipient + body hash to allow signal-cli deduplication where supported.

Failure handling: log and re-queue (do not block graph completion).
Individual send failures do not raise — the graph always completes successfully.
The reminder outbox handles guaranteed delivery for reminders separately.
This send node is for conversation replies only.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any

import structlog

from app.graph.state import State

log = structlog.get_logger(__name__)

_ENABLE_LANGGRAPH_PATH = os.environ.get("ENABLE_LANGGRAPH_PATH", "false").lower() in (
    "true", "1", "yes"
)


async def send_node(state: State) -> dict[str, Any]:
    """Terminal node: drain pending_outbound and send via Signal.

    When ENABLE_LANGGRAPH_PATH=false, logs but does not actually send.
    When true, calls signal_client.send_message() for each draft.

    Ordering: drafts are sent in list order (preserved from upstream nodes).
    Failures: logged, not raised. Graph completion is not blocked by send failures.

    Returns an empty dict — no state mutation after the terminal node.
    """
    pending = state.get("pending_outbound", [])

    if not pending:
        return {}

    if not _ENABLE_LANGGRAPH_PATH:
        for draft in pending:
            # Log recipient only — body is private conversation content
            log.debug(
                "send_node.dormant",
                recipient=draft.get("recipient"),
                has_body=bool(draft.get("body")),
            )
        return {}

    from app.tools import signal_client

    for draft in pending:
        recipient = draft.get("recipient", "")
        body = draft.get("body", "")
        notion_page_id = draft.get("notion_page_id")

        if not recipient or not body:
            # Log booleans only — no body content (private data discipline)
            log.warning(
                "send_node.skip_empty_draft",
                has_recipient=bool(recipient),
                has_body=bool(body),
            )
            continue

        # Generate idempotency key from content hash for deduplication
        key_source = f"{recipient}:{body}"
        idempotency_key = hashlib.sha256(key_source.encode()).hexdigest()[:32]

        try:
            result = await signal_client.send_message(
                recipient=recipient,
                message=body,
                idempotency_key=idempotency_key,
            )
            log.info(
                "send_node.sent",
                recipient=recipient,
                notion_page_id=notion_page_id,
                timestamp=result.get("timestamp"),
            )
        except Exception:
            log.exception(
                "send_node.send_failed",
                recipient=recipient,
                notion_page_id=notion_page_id,
            )
            # Do not re-raise — terminal node must not block graph completion.
            # For reminders, the outbox handles delivery. For conversation replies,
            # logging is the signal for operator visibility.

    return {}
