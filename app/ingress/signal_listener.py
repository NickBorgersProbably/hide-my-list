"""Signal inbound message listener.

Consumes the signal-cli-rest-api WebSocket stream and routes each
(peer, text) pair through the LangGraph pipeline, and each reaction
to the reward-feedback handler.

Authorization: AUTHORIZED_PEERS is a comma-separated env var of allowed
E.164 numbers. Messages from any other peer are silently dropped — no
reply is sent so the channel reveals no signal-cli liveness to an
attacker who happened to discover the bot's number. An empty or unset
AUTHORIZED_PEERS refuses startup; the fail-safe default is closed.

Authorization is enforced BEFORE dispatch to either the text or reaction
path. Reactions from unauthorized peers are dropped without DB writes.

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


def _extract_reaction(envelope: dict[str, Any]) -> tuple[str, str, int] | None:
    """Extract (peer, emoji, target_sent_timestamp) from a signal-cli reaction envelope.

    Returns None if the envelope is not a reaction, or if the reaction is a
    removal (isRemove: true). Removals are not recorded — only the first
    reaction for a given reward counts.

    signal-cli reaction envelope shape:
        envelope.source                           — sender E.164
        envelope.dataMessage.reaction.emoji       — reaction emoji character(s)
        envelope.dataMessage.reaction.isRemove    — true when user un-reacted
        envelope.dataMessage.reaction.targetSentTimestamp — ms-since-epoch
    """
    envelope_data = envelope.get("envelope", {})
    source = envelope_data.get("source", "")
    if not source:
        return None

    data_message = envelope_data.get("dataMessage", {})
    if data_message is None:
        return None

    reaction = data_message.get("reaction")
    if not reaction:
        return None

    # Skip un-react events — only the initial reaction counts.
    if reaction.get("isRemove", False):
        return None

    emoji = reaction.get("emoji", "")
    if not emoji:
        return None

    target_sent_timestamp = reaction.get("targetSentTimestamp")
    if target_sent_timestamp is None:
        return None

    try:
        ts_int = int(target_sent_timestamp)
    except (TypeError, ValueError, OverflowError):
        return None

    return source, emoji, ts_int


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

    async def _handle_reaction(
        self,
        peer: str,
        emoji: str,
        target_sent_timestamp: int,
    ) -> None:
        """Route a Signal reaction to the reward feedback handler.

        This is NOT a graph invocation. Reactions are administrative events
        that write directly to the reward_manifests Postgres table.
        """
        from app.tools.rewards import record_reward_feedback

        try:
            matched = await record_reward_feedback(
                peer=peer,
                emoji=emoji,
                target_sent_timestamp=target_sent_timestamp,
            )
            if matched:
                log.info("signal_listener.reaction_feedback_recorded")
            else:
                log.debug("signal_listener.reaction_no_matching_reward")
        except Exception:
            log.exception("signal_listener.reaction_feedback_error")

    async def run(self) -> None:
        """Main loop: consume WebSocket, route each envelope to the appropriate handler.

        Dispatch order:
        1. Try to extract a reaction. If found, route to _handle_reaction.
        2. Otherwise, try to extract a text message. If found, invoke the graph.
        3. Authorization is checked BEFORE either dispatch path — unauthorized
           peers are dropped here with no DB writes and no graph invocations.
        """
        graph = self._get_graph()
        log.info(
            "signal_listener.started",
            authorized_peer_count=len(self._authorized_peers),
        )

        async for envelope in receive_messages(
            base_url=self._base_url,
            account=self._account,
        ):
            # --- Reaction path ---
            reaction_result = _extract_reaction(envelope)
            if reaction_result is not None:
                peer, emoji, target_sent_timestamp = reaction_result
                if peer not in self._authorized_peers:
                    log.warning("signal_listener.unauthorized_peer_dropped")
                    continue
                await self._handle_reaction(peer, emoji, target_sent_timestamp)
                continue

            # --- Text message path ---
            text_result = _extract_peer_and_text(envelope)
            if text_result is None:
                continue

            peer, text = text_result

            if peer not in self._authorized_peers:
                log.warning("signal_listener.unauthorized_peer_dropped")
                continue

            log.info("signal_listener.message_received", peer=peer[:4] + "***")

            try:
                await graph.ainvoke(
                    {"peer": peer, "incoming": text},
                    config={"configurable": {"thread_id": peer}},
                )
            except Exception:
                log.exception("signal_listener.graph_error", peer=peer[:4] + "***")
