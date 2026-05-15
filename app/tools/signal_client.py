"""Signal CLI REST API client.

Async HTTP + WebSocket client wrapping bbernhard/signal-cli-rest-api.
Provides message send and inbound message streaming via WebSocket.

This module is one of three authorised sites for httpx.AsyncClient usage
(alongside app/tools/notion.py and app/ingress/signal_listener.py).
"""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import structlog
import websockets
import websockets.exceptions

log = structlog.get_logger(__name__)

_DEFAULT_BASE_URL = "http://localhost:8080"
_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 60.0
_MAX_RETRIES = 5
_RETRY_BASE_DELAY = 1.0  # seconds; exponential backoff


def _signal_base_url() -> str:
    return os.environ.get("SIGNAL_CLI_URL", _DEFAULT_BASE_URL)


def _account() -> str:
    return os.environ["SIGNAL_ACCOUNT"]


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


async def send_message(
    recipient: str,
    message: str,
    *,
    idempotency_key: str | None = None,
    base_url: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """Send a Signal message to recipient via signal-cli REST API.

    Args:
        recipient: E.164 phone number of the recipient.
        message: Text body to send.
        idempotency_key: Unique string passed as the mentions/sticker field
            placeholder for deduplication where signal-cli supports it.
            Signal-cli does not natively deduplicate sends by key, but storing
            and logging the key enables operator-level duplicate detection.
        base_url: Override for SIGNAL_CLI_URL (used in tests).
        account: Override for SIGNAL_ACCOUNT (used in tests).

    Returns:
        Parsed JSON response from signal-cli.
    """
    url_base = base_url or _signal_base_url()
    acct = account or _account()

    payload: dict[str, Any] = {
        "message": message,
        "number": acct,
        "recipients": [recipient],
    }
    # Log idempotency_key for operator-level duplicate detection.
    # Signal-cli REST API does not support client-side deduplication;
    # the key enables tracing and reconciliation when duplicates occur.
    if idempotency_key:
        log.debug("signal_client.send", idempotency_key=idempotency_key)

    async with httpx.AsyncClient(
        base_url=url_base,
        timeout=httpx.Timeout(connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT, write=30.0, pool=10.0),
    ) as client:
        delay = _RETRY_BASE_DELAY
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await client.post("/v2/send", json=payload)
                resp.raise_for_status()
                return resp.json()  # type: ignore[no-any-return]
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                    raise
                log.warning(
                    "signal_client.send.retry",
                    attempt=attempt + 1,
                    error=str(exc),
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
        raise RuntimeError(f"send_message failed after {_MAX_RETRIES} attempts") from last_exc


# ---------------------------------------------------------------------------
# Receive — WebSocket consumer
# ---------------------------------------------------------------------------


async def receive_messages(
    *,
    base_url: str | None = None,
    account: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Async generator yielding inbound message dicts from signal-cli WebSocket.

    Each yielded item is the parsed JSON envelope from signal-cli.
    Reconnects on WebSocket errors with exponential backoff.

    Args:
        base_url: Override for SIGNAL_CLI_URL (used in tests).
        account: Override for SIGNAL_ACCOUNT (used in tests).
    """
    url_base = base_url or _signal_base_url()
    acct = account or _account()

    # Build WebSocket URL: http://... -> ws://..., https://... -> wss://...
    ws_url = url_base.replace("https://", "wss://").replace("http://", "ws://")
    ws_endpoint = f"{ws_url}/v1/receive/{acct}"

    delay = _RETRY_BASE_DELAY
    while True:
        try:
            async with websockets.connect(ws_endpoint) as ws:
                delay = _RETRY_BASE_DELAY  # reset on successful connect
                async for raw_message in ws:
                    if not raw_message:
                        continue
                    try:
                        yield json.loads(raw_message)
                    except json.JSONDecodeError:
                        log.warning("signal_client.receive.bad_json")
        except (
            websockets.exceptions.WebSocketException,
            OSError,
        ) as exc:
            log.warning("signal_client.receive.reconnect", error=str(exc), delay=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
