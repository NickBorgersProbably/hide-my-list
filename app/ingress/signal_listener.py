"""Signal inbound message listener.

Consumes the signal-cli-rest-api WebSocket stream and routes each
(peer, text) pair through the LangGraph pipeline.

Authorization: AUTHORIZED_PEERS is a comma-separated env var of allowed
E.164 numbers. Messages from any other peer are silently dropped — no
reply is sent so the channel reveals no signal-cli liveness to an
attacker who happened to discover the bot's number. An empty or unset
AUTHORIZED_PEERS refuses startup; the fail-safe default is closed.

This module is one of three authorised sites for httpx.AsyncClient usage.
"""
from __future__ import annotations

import os
from typing import Any

import structlog

from app.tools.signal_client import receive_messages

log = structlog.get_logger(__name__)


def _load_authorized_peers() -> frozenset[str]:
    """Read AUTHORIZED_PEERS env var; return the frozenset of E.164 strings.

    Raises RuntimeError if the env var is missing or yields no usable peers
    after parsing — open ingress against single-tenant Notion data is not a
    default we'll ship.
    """
    raw = os.environ.get("AUTHORIZED_PEERS", "")
    peers = frozenset(p.strip() for p in raw.split(",") if p.strip())
    if not peers:
        raise RuntimeError(
            "AUTHORIZED_PEERS is empty or unset. Refusing to start: any peer "
            "that knows the signal-cli account number could otherwise read "
            "tasks from the single-tenant Notion database. Set "
            "AUTHORIZED_PEERS to a comma-separated list of E.164 numbers."
        )
    return peers


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
        authorized_peers: frozenset[str] | None = None,
    ) -> None:
        self._base_url = base_url
        self._account = account
        self._graph = graph  # injected in tests; built lazily in production
        # Eagerly load on construction so a misconfiguration fails fast at
        # startup instead of at the first inbound message.
        self._authorized_peers = (
            authorized_peers if authorized_peers is not None
            else _load_authorized_peers()
        )

    def _get_graph(self) -> Any:
        if self._graph is not None:
            return self._graph
        # Lazy import to avoid circular deps at module load
        from app.graph.graph import build_graph
        return build_graph()

    async def run(self) -> None:
        """Main loop: consume WebSocket, route each message to the graph."""
        graph = self._get_graph()
        log.info(
            "signal_listener.started",
            authorized_peer_count=len(self._authorized_peers),
        )

        async for envelope in receive_messages(
            base_url=self._base_url,
            account=self._account,
        ):
            result = _extract_peer_and_text(envelope)
            if result is None:
                continue

            peer, text = result

            if peer not in self._authorized_peers:
                # Silent drop. Logging the peer prefix only — the full number
                # would itself be useful intel for an attacker enumerating
                # numbers via timing. No response means an attacker cannot
                # confirm the bot is live by sending a message.
                log.warning(
                    "signal_listener.unauthorized_peer_dropped",
                    peer_prefix=peer[:4] + "***",
                )
                continue

            log.info("signal_listener.message_received", peer=peer[:4] + "***")

            try:
                await graph.ainvoke(
                    {"peer": peer, "incoming": text},
                    config={"configurable": {"thread_id": peer}},
                )
            except Exception:
                log.exception("signal_listener.graph_error", peer=peer[:4] + "***")
