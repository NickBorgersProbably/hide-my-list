"""Durable liveness markers for Signal ingress.

The receive WebSocket can be connected while the product is not receiving
usable inbound traffic. This module stores the last accepted inbound timestamp
in Postgres so scheduler checks survive restarts and crash loops.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import structlog

from app.tools.db import get_db_conn

log = structlog.get_logger(__name__)

_ROW_NAME = "default"
_DEFAULT_INBOUND_SILENCE_THRESHOLD_SECONDS = 36 * 60 * 60


def _now() -> datetime:
    return datetime.now(UTC)


def _silence_threshold_seconds() -> int:
    raw = os.environ.get(
        "SIGNAL_INBOUND_SILENCE_ALERT_THRESHOLD_SECONDS",
        str(_DEFAULT_INBOUND_SILENCE_THRESHOLD_SECONDS),
    )
    try:
        value = int(raw)
    except ValueError:
        log.warning(
            "signal_ingress_health.invalid_silence_threshold",
            configured_value=raw,
            fallback_seconds=_DEFAULT_INBOUND_SILENCE_THRESHOLD_SECONDS,
        )
        return _DEFAULT_INBOUND_SILENCE_THRESHOLD_SECONDS
    if value <= 0:
        log.warning(
            "signal_ingress_health.invalid_silence_threshold",
            configured_value=raw,
            fallback_seconds=_DEFAULT_INBOUND_SILENCE_THRESHOLD_SECONDS,
        )
        return _DEFAULT_INBOUND_SILENCE_THRESHOLD_SECONDS
    return value


def _format_duration(delta: timedelta) -> str:
    total_seconds = max(0, int(delta.total_seconds()))
    days, remainder = divmod(total_seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, _ = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


async def record_inbound_message(*, received_at: datetime | None = None) -> None:
    """Persist that an authorized inbound Signal item reached the app."""
    timestamp = received_at or _now()
    async with get_db_conn() as conn:
        await conn.execute(
            """
            INSERT INTO signal_ingress_health (name, last_inbound_at, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (name)
            DO UPDATE SET
              last_inbound_at = EXCLUDED.last_inbound_at,
              updated_at = EXCLUDED.updated_at
            """,
            (_ROW_NAME, timestamp, timestamp),
        )
    log.info("signal_ingress_health.recorded")


async def check_inbound_silence(*, now: datetime | None = None) -> bool:
    """Enqueue a throttled ops alert when Signal ingress has been quiet too long.

    Returns True when an alert was enqueued and False otherwise.
    """
    from app.tools import ops_alerts

    checked_at = now or _now()
    threshold_seconds = _silence_threshold_seconds()
    threshold = timedelta(seconds=threshold_seconds)

    async with get_db_conn() as conn:
        cursor = await conn.execute(
            """
            SELECT last_inbound_at
            FROM signal_ingress_health
            WHERE name = %s
            """,
            (_ROW_NAME,),
        )
        row = await cursor.fetchone()

    if row is None:
        await ops_alerts.enqueue(
            kind="signal_ingress_silent",
            body=(
                "Signal ingress has no durable last-inbound marker; "
                "silence duration is unknown and requires investigation."
            ),
            severity="critical",
        )
        log.warning("signal_ingress_health.missing_marker_alerted")
        return True

    last_inbound_at: datetime = row["last_inbound_at"]
    silence_duration = checked_at - last_inbound_at
    if silence_duration <= threshold:
        log.debug(
            "signal_ingress_health.ok",
            silence_seconds=int(silence_duration.total_seconds()),
            threshold_seconds=threshold_seconds,
        )
        return False

    duration_text = _format_duration(silence_duration)
    threshold_text = _format_duration(threshold)
    await ops_alerts.enqueue(
        kind="signal_ingress_silent",
        body=(
            "Signal ingress has been silent for "
            f"{duration_text}; threshold is {threshold_text}. "
            f"Last inbound at {last_inbound_at.isoformat()}."
        ),
        severity="critical",
    )
    log.warning(
        "signal_ingress_health.silence_alerted",
        silence_seconds=int(silence_duration.total_seconds()),
        threshold_seconds=threshold_seconds,
    )
    return True
