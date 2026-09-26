"""Signal inbound message listener.

Consumes the signal-cli-rest-api WebSocket stream and routes each
(peer, text) pair through the LangGraph pipeline.

Authorization: AUTHORIZED_PEERS is a comma-separated env var of allowed
E.164 numbers. Messages from any other peer are silently dropped — no
reply is sent so the channel reveals no signal-cli liveness to an
attacker who happened to discover the bot's number. An empty or unset
AUTHORIZED_PEERS refuses startup; the fail-safe default is closed.

After each turn the listener schedules the post-send interaction review
(`app/graph/interaction_review.py`) as a background task per peer, backed by a
durable `pending` row in `interaction_reviews`. It yields to live
conversation: it starts after a short delay, is skipped when the peer already
has a message waiting, and is cancelled when the peer's next message is picked
up. Once it has started writing, that next turn waits for it — at most
`_REVIEW_EXECUTION_WAIT_SECONDS`, after which the review is cancelled — so no
review writes after the next turn starts. Every cancellation finalizes the row
as skipped. On startup, rows a stopped process left unfinished are settled:
a `pending` row (never claimed) is reviewed again when its turn is still the
peer's latest checkpoint and retired otherwise; an `executing` row (claimed,
effects possibly run) is finalized `error(interrupted)` and never replayed.

This module is one of three authorised sites for httpx.AsyncClient usage.
"""
from __future__ import annotations

import asyncio
import functools
import os
import uuid
from collections import deque
from collections.abc import Coroutine, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from app.tools.signal_client import (
    receive_messages,
    send_message,
    send_read_receipt,
    send_typing_indicator,
)
from app.tools.signal_ingress_health import record_inbound_message

log = structlog.get_logger(__name__)

_TYPING_REFRESH_SECONDS = 10.0
_MESSAGE_DEBOUNCE_SECONDS = 3.0
_DEFAULT_QUEUE_DEPTH = 32
# Longest the next turn waits for a review that has already started writing;
# past it the review is cancelled and the turn runs.
_REVIEW_EXECUTION_WAIT_SECONDS = 60.0
# A pending review older than this at startup is retired, not resumed: its
# follow-up would arrive too long after the turn to make sense.
_REVIEW_RESUME_MAX_AGE_SECONDS = 3600.0
# Bound on the startup read of pending reviews, so a slow database cannot hold
# up the WebSocket consumer.
_REVIEW_RESUME_TIMEOUT_SECONDS = 10.0
_OVERFLOW_REPLY = (
    "I'm catching up and can't take more messages right now. Please try again in a minute."
)


@dataclass(frozen=True)
class _QueuedMessage:
    peer: str
    text: str


@dataclass
class _ReviewJob:
    """One peer's scheduled review: its task, its row, and whether it is writing."""

    review_id: uuid.UUID | None = None
    turn_ref: str = ""
    executing: bool = False
    task: asyncio.Task[None] | None = field(default=None, repr=False)


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


def _extract_peer_and_text(envelope: dict[str, Any]) -> tuple[str, str, int | None] | None:
    """Extract (sender_e164, text, timestamp) from a signal-cli envelope dict.

    Returns None if the envelope is not a text message from a peer.
    """
    outer = envelope.get("envelope", {})
    data_message = outer.get("dataMessage", {})
    text = data_message.get("message", "")
    if not text:
        return None

    source = outer.get("source", "")
    if not source:
        return None

    timestamp = outer.get("timestamp")
    if not isinstance(timestamp, int):
        timestamp = data_message.get("timestamp")
    if not isinstance(timestamp, int):
        timestamp = None

    return source, text, timestamp


def _log_background_task_result(task: asyncio.Task[None]) -> None:
    """Consume unexpected background task exceptions without leaking content."""
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception as exc:
        log.warning("signal_listener.background_task_failed", error_type=type(exc).__name__)


def _start_background_task(coro: Coroutine[Any, Any, None]) -> None:
    """Schedule a best-effort coroutine and log unexpected failures."""
    task = asyncio.create_task(coro)
    task.add_done_callback(_log_background_task_result)


async def _record_inbound_activity() -> None:
    """Best-effort durable marker that authorized Signal ingress is alive."""
    try:
        await record_inbound_message()
    except Exception as exc:
        log.warning(
            "signal_listener.ingress_health_record_failed",
            error_type=type(exc).__name__,
        )


async def _maintain_typing_indicator(
    *,
    peer: str,
    stop_event: asyncio.Event,
    base_url: str | None,
    account: str | None,
    refresh_seconds: float = _TYPING_REFRESH_SECONDS,
) -> None:
    """Refresh Signal typing indicator until stop_event is set."""
    while not stop_event.is_set():
        await send_typing_indicator(
            peer,
            started=True,
            base_url=base_url,
            account=account,
        )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=refresh_seconds)
        except TimeoutError:
            continue


class _InboundMessageBuffer:
    """Bounded async buffer for text messages waiting on the serial graph worker."""

    def __init__(self, max_depth: int) -> None:
        if max_depth <= 0:
            raise ValueError("max_depth must be positive")
        self._max_depth = max_depth
        self._pending: deque[_QueuedMessage] = deque()
        self._closed = False
        self._condition = asyncio.Condition()

    @property
    def max_depth(self) -> int:
        return self._max_depth

    async def try_add(self, message: _QueuedMessage) -> bool:
        """Add a message if capacity remains; return False on overflow."""
        async with self._condition:
            if self._closed or len(self._pending) >= self._max_depth:
                return False
            self._pending.append(message)
            self._condition.notify()
            return True

    def has_pending(self, peer: str) -> bool:
        """Whether a message from `peer` is waiting to be processed."""
        return any(message.peer == peer for message in self._pending)

    async def get(self) -> _QueuedMessage | None:
        """Pop the next message, or None once the buffer is closed and empty."""
        async with self._condition:
            while not self._pending and not self._closed:
                await self._condition.wait()
            if not self._pending:
                return None
            return self._pending.popleft()

    async def collect_peer(self, peer: str) -> list[_QueuedMessage]:
        """Remove all currently pending messages from one peer, preserving order."""
        async with self._condition:
            matching: list[_QueuedMessage] = []
            remaining: deque[_QueuedMessage] = deque()
            while self._pending:
                message = self._pending.popleft()
                if message.peer == peer:
                    matching.append(message)
                else:
                    remaining.append(message)
            self._pending = remaining
            return matching

    async def close(self) -> None:
        """Stop accepting messages and let the worker drain what remains."""
        async with self._condition:
            self._closed = True
            self._condition.notify_all()


def _extract_reaction(envelope: dict[str, Any]) -> tuple[str, str, int, str] | None:
    """Extract reaction feedback fields from a signal-cli envelope.

    Returns None for non-reaction envelopes, removed reactions, or malformed
    reaction payloads.
    """
    outer = envelope.get("envelope", {})
    data_message = outer.get("dataMessage", {})
    reaction = data_message.get("reaction")
    if not isinstance(reaction, dict):
        return None

    if reaction.get("isRemove") is True:
        return None

    source = outer.get("source", "")
    emoji = reaction.get("emoji", "")
    target_author = reaction.get("targetAuthor", "")
    target_sent_timestamp = reaction.get("targetSentTimestamp")
    if (
        not source
        or not emoji
        or not target_author
        or not isinstance(target_sent_timestamp, int)
    ):
        return None

    return source, emoji, target_sent_timestamp, target_author


def _target_author_matches_account(target_author: str, account: str | None) -> bool:
    """Return whether a reaction targeted a bot-authored message."""
    expected_account = account or os.environ.get("SIGNAL_ACCOUNT", "")
    return bool(expected_account) and target_author == expected_account


class SignalListener:
    """Asyncio task that consumes signal-cli WebSocket and drives the graph."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        account: str | None = None,
        graph: Any = None,
        authorized_peers: frozenset[str] | None = None,
        message_debounce_seconds: float = _MESSAGE_DEBOUNCE_SECONDS,
        queue_depth: int = _DEFAULT_QUEUE_DEPTH,
        interaction_review_enabled: bool | None = None,
        interaction_review_delay_seconds: float | None = None,
    ) -> None:
        from app.graph.interaction_review import review_settings

        self._base_url = base_url
        self._account = account
        self._graph = graph  # injected in tests; built lazily in production
        self._message_debounce_seconds = message_debounce_seconds
        self._message_buffer = _InboundMessageBuffer(queue_depth)
        settings = review_settings()
        self._review_enabled = (
            settings.enabled if interaction_review_enabled is None
            else interaction_review_enabled
        )
        self._review_delay_seconds = (
            settings.delay_seconds if interaction_review_delay_seconds is None
            else interaction_review_delay_seconds
        )
        # One scheduled review per peer.
        self._review_jobs: dict[str, _ReviewJob] = {}
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

    async def _send_overflow_reply(self, peer: str) -> None:
        """Tell the authorized sender that this message could not be queued."""
        try:
            await send_message(
                peer,
                _OVERFLOW_REPLY,
                base_url=self._base_url,
                account=self._account,
            )
        except Exception as exc:
            log.warning(
                "signal_listener.queue_overflow_reply_failed",
                error_type=type(exc).__name__,
            )

    # -- post-send interaction review ---------------------------------------

    def _start_review(
        self,
        *,
        graph: Any,
        peer: str,
        final_state: Any,
        config: dict[str, Any],
        review_id: uuid.UUID | None = None,
        turn_ref: str = "",
    ) -> None:
        """Schedule the review of the turn that just finished. Never awaits.

        The task's first step stores the pending row, unless `review_id` names
        one already stored (a review resumed on startup).
        """
        if not self._review_enabled or not isinstance(final_state, Mapping):
            return
        job = _ReviewJob(review_id=review_id, turn_ref=turn_ref)
        job.task = asyncio.create_task(
            self._run_review(
                job, graph=graph, peer=peer, final_state=final_state, config=config
            )
        )
        self._review_jobs[peer] = job
        job.task.add_done_callback(functools.partial(self._forget_review, peer, job))

    def _forget_review(self, peer: str, job: _ReviewJob, task: asyncio.Task[None]) -> None:
        if self._review_jobs.get(peer) is job:
            del self._review_jobs[peer]
        _log_background_task_result(task)

    def _claim_review_execution(self, peer: str, job: _ReviewJob) -> bool:
        """Let a review start writing unless the peer has a message waiting."""
        if self._message_buffer.has_pending(peer):
            return False
        job.executing = True
        return True

    async def _create_pending_review(
        self, job: _ReviewJob, *, graph: Any, peer: str, final_state: Mapping[str, Any],
        config: dict[str, Any],
    ) -> bool:
        """Store the job's pending row; False when it could not be stored.

        Reading `turn_ref` and inserting the row are shielded as one step:
        when the task is cancelled meanwhile, the row still lands, its id is
        kept on the job, and the cancelling side finalizes it. So every
        scheduled review leaves a row, however early it is cancelled.
        """
        from app.graph.interaction_review import current_turn_ref
        from app.tools import interaction_reviews

        async def insert() -> uuid.UUID:
            job.turn_ref = await current_turn_ref(graph, config)
            return await interaction_reviews.create_pending(
                peer=peer,
                turn_ref=job.turn_ref,
                intent=str(final_state.get("intent") or "") or None,
            )

        creating = asyncio.ensure_future(insert())
        try:
            job.review_id = await asyncio.shield(creating)
        except asyncio.CancelledError:
            try:
                job.review_id = await creating
            except Exception as exc:
                log.warning(
                    "interaction_review.pending_store_failed", error_type=type(exc).__name__
                )
            raise
        except Exception as exc:
            # No durable row, no review: a correction must leave a record.
            log.warning(
                "interaction_review.pending_store_failed", error_type=type(exc).__name__
            )
            return False
        return True

    async def _run_review(
        self,
        job: _ReviewJob,
        *,
        graph: Any,
        peer: str,
        final_state: Mapping[str, Any],
        config: dict[str, Any],
    ) -> None:
        from app.graph.interaction_review import review_turn

        if job.review_id is None and not await self._create_pending_review(
            job, graph=graph, peer=peer, final_state=final_state, config=config
        ):
            return
        assert job.review_id is not None
        if self._review_delay_seconds > 0:
            await asyncio.sleep(self._review_delay_seconds)
        await review_turn(
            peer=peer,
            final_state=final_state,
            graph=graph,
            config=config,
            review_id=job.review_id,
            turn_ref=job.turn_ref,
            still_current=lambda: not self._message_buffer.has_pending(peer),
            claim_execution=lambda: self._claim_review_execution(peer, job),
        )

    async def _stop_review(self, job: _ReviewJob, *, reason: str) -> None:
        """Cancel a review, wait for it to stop, and finalize its row as skipped.

        The review finalizes its own row when its handler runs; this second,
        idempotent finalize covers a cancel that lands before `review_turn`
        starts (the insert or the start delay).
        """
        from app.graph.interaction_review import finalize_skipped

        task = job.task
        if task is None:
            return
        if not task.done():
            task.cancel(msg=reason)
            await asyncio.wait({task})
        await finalize_skipped(job.review_id, reason=reason)

    async def _yield_review(self, peer: str) -> None:
        """Make way for the peer's next turn.

        A review that has not started writing is cancelled. One that has is
        awaited up to `_REVIEW_EXECUTION_WAIT_SECONDS`, so the next turn reads
        the checkpoint it writes; past that bound it is cancelled. Either way
        the review has stopped before this returns, so it never writes during
        or after the next turn.
        """
        job = self._review_jobs.get(peer)
        if job is None or job.task is None or job.task.done():
            return
        if job.executing:
            done, _ = await asyncio.wait({job.task}, timeout=_REVIEW_EXECUTION_WAIT_SECONDS)
            if done:
                return
            log.warning("signal_listener.review_wait_timed_out")
            await self._stop_review(job, reason="timeout")
            return
        await self._stop_review(job, reason="cancelled")

    async def wait_for_review(self, peer: str) -> None:
        """Wait until the peer's scheduled review (if any) has finished.

        For tests: the review runs in the background, so a harness settles it
        explicitly before asserting on its effects.
        """
        job = self._review_jobs.get(peer)
        if job is not None and job.task is not None:
            await asyncio.wait({job.task})

    async def _cancel_reviews(self) -> None:
        jobs = list(self._review_jobs.values())
        if jobs:
            await asyncio.gather(
                *(self._stop_review(job, reason="cancelled") for job in jobs),
                return_exceptions=True,
            )

    async def _resume_pending_reviews(self) -> None:
        """Settle the reviews a stopped process left unfinished.

        An `executing` row was claimed: its correction may have written Notion
        and sent its follow-up. It is finalized `error(interrupted)` with the
        action and page it recorded, and never reviewed again, so no effect
        repeats. A `pending` row was never claimed. It is resumed when reviews
        are on, it is younger than `_REVIEW_RESUME_MAX_AGE_SECONDS`, and its
        `turn_ref` is still the peer's latest checkpoint; the review then runs
        from that checkpoint's state. Every other pending row is finalized as
        skipped (`disabled` or `superseded`). Only authorized peers' rows are
        touched. Best effort: a failure here is logged and startup continues.
        """
        from app.graph.interaction_review import (
            checkpoint_id_of,
            finalize_interrupted,
            finalize_skipped,
        )
        from app.tools import interaction_reviews

        try:
            rows = await asyncio.wait_for(
                interaction_reviews.list_unfinished(), timeout=_REVIEW_RESUME_TIMEOUT_SECONDS
            )
        except Exception as exc:
            log.warning("interaction_review.resume_failed", error_type=type(exc).__name__)
            return
        rows = [row for row in rows if row.get("peer") in self._authorized_peers]
        claimed = [row for row in rows if row.get("verdict") == "executing"]
        for row in claimed:
            await finalize_interrupted(row["id"])
        if claimed:
            log.info("interaction_review.interrupted", count=len(claimed))
        rows = [row for row in rows if row.get("verdict") == "pending"]
        if not rows:
            return

        graph = self._get_graph()
        now = datetime.now(UTC)
        resumed = retired = 0
        # Newest first: only one row per peer can match its latest checkpoint.
        for row in sorted(rows, key=lambda r: r["created_at"], reverse=True):
            peer = str(row["peer"])
            turn_ref = str(row.get("turn_ref") or "")
            config: dict[str, Any] = {"configurable": {"thread_id": peer}}
            reason: str | None = None
            values: Any = None
            if not self._review_enabled:
                reason = "disabled"
            elif (
                peer in self._review_jobs
                or not turn_ref
                or (now - row["created_at"]).total_seconds() > _REVIEW_RESUME_MAX_AGE_SECONDS
            ):
                reason = "superseded"
            else:
                try:
                    snapshot = await graph.aget_state(config)
                except Exception:
                    snapshot = None
                values = getattr(snapshot, "values", None)
                if checkpoint_id_of(snapshot) != turn_ref or not isinstance(values, Mapping):
                    reason = "superseded"
            if reason is not None:
                await finalize_skipped(row["id"], reason=reason)
                retired += 1
                continue
            self._start_review(
                graph=graph,
                peer=peer,
                final_state=dict(values),
                config=config,
                review_id=row["id"],
                turn_ref=turn_ref,
            )
            resumed += 1
        log.info("interaction_review.resumed", count=resumed)
        log.info("interaction_review.resume_skipped", count=retired)

    async def _invoke_graph_for_messages(
        self,
        *,
        graph: Any,
        peer: str,
        messages: list[_QueuedMessage],
    ) -> None:
        incoming = "\n".join(message.text for message in messages)
        typing_stop = asyncio.Event()
        _start_background_task(
            _maintain_typing_indicator(
                peer=peer,
                stop_event=typing_stop,
                base_url=self._base_url,
                account=self._account,
            )
        )
        config: dict[str, Any] = {"configurable": {"thread_id": peer}}
        try:
            final_state = await graph.ainvoke(
                {"peer": peer, "incoming": incoming},
                config=config,
            )
            # Scheduled before any await so a caller woken by the turn's end
            # already sees the review registered.
            self._start_review(graph=graph, peer=peer, final_state=final_state, config=config)
            if len(messages) > 1:
                log.info(
                    "signal_listener.messages_coalesced",
                    peer="<recipient>",
                    message_count=len(messages),
                )
        except Exception:
            log.exception("signal_listener.graph_error", peer="<recipient>")
        finally:
            typing_stop.set()
            _start_background_task(
                send_typing_indicator(
                    peer,
                    started=False,
                    base_url=self._base_url,
                    account=self._account,
                )
            )

    async def _process_messages(self) -> None:
        """Drain queued text messages serially, coalescing rapid same-peer sends."""
        graph: Any | None = None
        while True:
            first = await self._message_buffer.get()
            if first is None:
                return

            # The peer spoke again: the previous turn's review yields first.
            await self._yield_review(first.peer)

            if self._message_debounce_seconds > 0:
                await asyncio.sleep(self._message_debounce_seconds)

            messages = [first]
            messages.extend(await self._message_buffer.collect_peer(first.peer))

            if graph is None:
                graph = self._get_graph()
            await self._invoke_graph_for_messages(
                graph=graph,
                peer=first.peer,
                messages=messages,
            )

    async def run(self) -> None:
        """Main loop: consume WebSocket, route each message to the graph."""
        log.info(
            "signal_listener.started",
            authorized_peer_count=len(self._authorized_peers),
        )
        await self._resume_pending_reviews()
        worker = asyncio.create_task(self._process_messages())
        worker.add_done_callback(_log_background_task_result)

        try:
            async for envelope in receive_messages(
                base_url=self._base_url,
                account=self._account,
            ):
                reaction = _extract_reaction(envelope)
                if reaction is not None:
                    peer, emoji, target_sent_timestamp, target_author = reaction
                    if peer not in self._authorized_peers:
                        log.warning("signal_listener.unauthorized_peer_dropped")
                        continue

                    if not _target_author_matches_account(target_author, self._account):
                        log.info("signal_listener.reaction_non_bot_target_dropped")
                        continue

                    await _record_inbound_activity()

                    from app.tools.rewards import record_reward_feedback

                    await record_reward_feedback(
                        peer=peer,
                        emoji=emoji,
                        target_sent_timestamp=target_sent_timestamp,
                    )
                    log.info("signal_listener.reaction_recorded")
                    continue

                result = _extract_peer_and_text(envelope)
                if result is None:
                    continue

                peer, text, timestamp = result

                if peer not in self._authorized_peers:
                    log.warning("signal_listener.unauthorized_peer_dropped")
                    continue

                await _record_inbound_activity()

                log.info("signal_listener.message_received", peer="<recipient>")

                if timestamp is not None:
                    _start_background_task(
                        send_read_receipt(
                            peer,
                            timestamp,
                            base_url=self._base_url,
                            account=self._account,
                        )
                    )
                else:
                    log.warning("signal_listener.receipt_skipped_missing_timestamp")

                queued = await self._message_buffer.try_add(
                    _QueuedMessage(peer=peer, text=text)
                )
                if queued:
                    continue

                log.warning(
                    "signal_listener.queue_overflow",
                    peer="<recipient>",
                    queue_depth=self._message_buffer.max_depth,
                )
                _start_background_task(
                    self._send_overflow_reply(peer)
                )
        except asyncio.CancelledError:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            await self._cancel_reviews()
            raise
        except Exception:
            await self._message_buffer.close()
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            await self._cancel_reviews()
            raise
        else:
            await self._message_buffer.close()
            await worker
