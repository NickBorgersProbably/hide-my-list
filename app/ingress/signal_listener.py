"""Signal inbound message listener.

Consumes the signal-cli-rest-api WebSocket stream and routes each
(peer, text) pair through the LangGraph pipeline.

This module is one of three authorised sites for httpx.AsyncClient usage.
"""
from __future__ import annotations

from typing import Any

import structlog

from app.tools.signal_client import receive_messages

log = structlog.get_logger(__name__)


def _extract_peer_and_text(envelope: dict[str, Any]) -> tuple[str, str] | None:
    """Extract (sender_e164, text) from a signal-cli envelope dict.

    Returns None if the envelope is not a text message from a peer.
    """
    data_message = (
        envelope.get("envelope", {})
        .get("dataMessage", {})
    )
    text = data_message.get("message", "")
    if not text:
        return None

    source = envelope.get("envelope", {}).get("source", "")
    if not source:
        return None

    return source, text


class SignalListener:
    """Asyncio task that consumes signal-cli WebSocket and drives the graph."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        account: str | None = None,
        graph: Any = None,
    ) -> None:
        self._base_url = base_url
        self._account = account
        self._graph = graph  # injected in tests; built lazily in production

    def _get_graph(self) -> Any:
        if self._graph is not None:
            return self._graph
        # Lazy import to avoid circular deps at module load
        from app.graph.graph import build_graph
        return build_graph()

    async def run(self) -> None:
        """Main loop: consume WebSocket, route each message to the graph."""
        graph = self._get_graph()
        log.info("signal_listener.started")

        async for envelope in receive_messages(
            base_url=self._base_url,
            account=self._account,
        ):
            result = _extract_peer_and_text(envelope)
            if result is None:
                continue

            peer, text = result
            log.info("signal_listener.message_received", peer=peer[:4] + "***")

            try:
                await graph.ainvoke(
                    {"peer": peer, "incoming": text},
                    config={"configurable": {"thread_id": peer}},
                )
            except Exception:
                log.exception("signal_listener.graph_error", peer=peer[:4] + "***")
