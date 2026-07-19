from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest


class _Cursor:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    async def fetchone(self) -> dict[str, Any] | None:
        return self._row


class _Conn:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    async def execute(self, _query: str, _params: tuple[Any, ...]) -> _Cursor:
        return _Cursor(self._row)


def _db_conn_for(row: dict[str, Any] | None):
    @asynccontextmanager
    async def _db_conn() -> AsyncIterator[_Conn]:
        yield _Conn(row)

    return _db_conn


@pytest.mark.asyncio
async def test_silence_detector_stays_quiet_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tools import ops_alerts, signal_ingress_health

    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    row = {"last_inbound_at": now - timedelta(hours=12)}
    enqueue = AsyncMock()

    monkeypatch.setenv("SIGNAL_INBOUND_SILENCE_ALERT_THRESHOLD_SECONDS", "86400")
    monkeypatch.setattr(signal_ingress_health, "get_db_conn", _db_conn_for(row))
    monkeypatch.setattr(ops_alerts, "enqueue", enqueue)

    alerted = await signal_ingress_health.check_inbound_silence(now=now)

    assert alerted is False
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_silence_detector_alerts_past_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tools import ops_alerts, signal_ingress_health

    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    row = {"last_inbound_at": now - timedelta(hours=49)}
    enqueue = AsyncMock()

    monkeypatch.setenv("SIGNAL_INBOUND_SILENCE_ALERT_THRESHOLD_SECONDS", "86400")
    monkeypatch.setattr(signal_ingress_health, "get_db_conn", _db_conn_for(row))
    monkeypatch.setattr(ops_alerts, "enqueue", enqueue)

    alerted = await signal_ingress_health.check_inbound_silence(now=now)

    assert alerted is True
    enqueue.assert_awaited_once()
    kwargs = enqueue.await_args.kwargs
    assert kwargs["kind"] == "signal_ingress_silent"
    assert kwargs["severity"] == "critical"
    assert "2d 1h" in kwargs["body"]
    assert "threshold is 1d" in kwargs["body"]
