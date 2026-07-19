"""Integration tests for Signal ingress liveness (migration 0010 + tool functions).

Verifies:
  - record_inbound_message upserts last_inbound_at in signal_ingress_health.
  - check_inbound_silence returns False and does not alert below threshold.
  - check_inbound_silence returns True and enqueues a critical alert above threshold.
  - check_inbound_silence returns True and enqueues an alert when no marker row exists.

Tests require a live DATABASE_URL. Skipped otherwise.

Private data discipline: all values use placeholders; no real recipients or content.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

_HAS_DB = bool(os.environ.get("DATABASE_URL", ""))
pytestmark = pytest.mark.skipif(
    not _HAS_DB,
    reason="DATABASE_URL not set; skipping integration tests",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db_conn() -> Any:
    """Provide a psycopg connection with all migrations applied and signal_ingress_health clean."""
    import psycopg

    conn_str = os.environ["DATABASE_URL"]
    async with await psycopg.AsyncConnection.connect(conn_str, autocommit=False) as conn:
        from app.tools.db import _MIGRATIONS_DIR

        for mig in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            await conn.execute(mig.read_text())  # type: ignore[arg-type]
        await conn.commit()

        await conn.execute("DELETE FROM signal_ingress_health")
        await conn.commit()

        yield conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_inbound_message_upserts_row(db_conn: Any) -> None:
    """record_inbound_message must write last_inbound_at to signal_ingress_health."""
    from app.tools.signal_ingress_health import record_inbound_message

    ts = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
    await record_inbound_message(received_at=ts)
    await db_conn.commit()

    cursor = await db_conn.execute(
        "SELECT last_inbound_at, updated_at FROM signal_ingress_health WHERE name = 'default'"
    )
    row = await cursor.fetchone()
    assert row is not None, "signal_ingress_health row not created"
    assert row["last_inbound_at"].replace(tzinfo=UTC) == ts


@pytest.mark.asyncio
async def test_record_inbound_message_upserts_on_second_call(db_conn: Any) -> None:
    """A second record_inbound_message call replaces the previous timestamp."""
    from app.tools.signal_ingress_health import record_inbound_message

    first = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
    second = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    await record_inbound_message(received_at=first)
    await record_inbound_message(received_at=second)
    await db_conn.commit()

    cursor = await db_conn.execute(
        "SELECT last_inbound_at FROM signal_ingress_health WHERE name = 'default'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["last_inbound_at"].replace(tzinfo=UTC) == second


@pytest.mark.asyncio
async def test_check_inbound_silence_no_alert_below_threshold(db_conn: Any) -> None:
    """check_inbound_silence returns False and does not alert when silence is below threshold."""
    from app.tools.signal_ingress_health import check_inbound_silence, record_inbound_message

    now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    await record_inbound_message(received_at=now - timedelta(hours=1))
    await db_conn.commit()

    enqueue = AsyncMock()
    with (
        patch.dict(os.environ, {"SIGNAL_INBOUND_SILENCE_ALERT_THRESHOLD_SECONDS": "86400"}),
        patch("app.tools.ops_alerts.enqueue", enqueue),
    ):
        alerted = await check_inbound_silence(now=now)

    assert alerted is False
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_inbound_silence_alerts_above_threshold(db_conn: Any) -> None:
    """check_inbound_silence returns True and enqueues a critical alert above threshold."""
    from app.tools.signal_ingress_health import check_inbound_silence, record_inbound_message

    now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    await record_inbound_message(received_at=now - timedelta(hours=40))
    await db_conn.commit()

    enqueue = AsyncMock()
    with (
        patch.dict(os.environ, {"SIGNAL_INBOUND_SILENCE_ALERT_THRESHOLD_SECONDS": "86400"}),
        patch("app.tools.ops_alerts.enqueue", enqueue),
    ):
        alerted = await check_inbound_silence(now=now)

    assert alerted is True
    enqueue.assert_awaited_once()
    kwargs = enqueue.await_args.kwargs
    assert kwargs["kind"] == "signal_ingress_silent"
    assert kwargs["severity"] == "critical"


@pytest.mark.asyncio
async def test_check_inbound_silence_alerts_when_no_marker_row(db_conn: Any) -> None:
    """check_inbound_silence alerts with unknown-duration message when no row exists."""
    from app.tools.signal_ingress_health import check_inbound_silence

    now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

    enqueue = AsyncMock()
    with patch("app.tools.ops_alerts.enqueue", enqueue):
        alerted = await check_inbound_silence(now=now)

    assert alerted is True
    enqueue.assert_awaited_once()
    kwargs = enqueue.await_args.kwargs
    assert kwargs["kind"] == "signal_ingress_silent"
    assert kwargs["severity"] == "critical"
    assert "unknown" in kwargs["body"]
