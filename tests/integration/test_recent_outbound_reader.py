"""Integration tests for the recent_outbound reader (issue #641).

Runs against a real Postgres database when DATABASE_URL is set; skipped
otherwise. The reader is what lets a completion reply be matched to the
reminder it answers, so the SQL predicates — awaiting_reply, expiry, per-peer
scoping, recency ordering — are the contract worth exercising against a real
database rather than a mock.

Private data discipline: placeholder page IDs, peers, and titles only.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.tools.recent_outbound import clear_awaiting_reply, load_awaiting_reply

_HAS_DB = bool(os.environ.get("DATABASE_URL", ""))
pytestmark = pytest.mark.skipif(
    not _HAS_DB,
    reason="DATABASE_URL not set; skipping integration tests",
)


async def _insert(
    *,
    peer: str,
    page_id: str,
    signal_timestamp: int,
    sent_ago: timedelta,
    expires_in: timedelta,
    awaiting_reply: bool = True,
) -> None:
    from app.tools.db import get_db_conn

    now = datetime.now(UTC)
    async with get_db_conn() as conn:
        await conn.execute(
            """
            INSERT INTO recent_outbound
              (peer, signal_timestamp, notion_page_id, reminder_type, title,
               prompt_kind, sent_at, awaiting_reply, expires_at)
            VALUES (%s, %s, %s, 'reminder', '<reminder body>', 'sent', %s, %s, %s)
            """,
            (
                peer,
                signal_timestamp,
                page_id,
                now - sent_ago,
                awaiting_reply,
                now + expires_in,
            ),
        )


@pytest.fixture()
async def peer() -> str:
    """A unique peer per test, cleaned up afterwards."""
    value = f"<test-{uuid.uuid4()}>"
    yield value
    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        await conn.execute("DELETE FROM recent_outbound WHERE peer = %s", (value,))


@pytest.mark.asyncio
async def test_returns_unresolved_reminder(peer: str) -> None:
    await _insert(
        peer=peer,
        page_id="<page_a>",
        signal_timestamp=1,
        sent_ago=timedelta(hours=3),
        expires_in=timedelta(hours=21),
    )

    entry = await load_awaiting_reply(peer)

    assert entry is not None
    assert entry["notion_page_id"] == "<page_a>"
    assert entry["signal_timestamp"] == 1


@pytest.mark.asyncio
async def test_ignores_expired_rows(peer: str) -> None:
    """Expiry is what stops day-old context matching an unrelated message."""
    await _insert(
        peer=peer,
        page_id="<page_a>",
        signal_timestamp=1,
        sent_ago=timedelta(days=2),
        expires_in=timedelta(hours=-1),
    )

    assert await load_awaiting_reply(peer) is None


@pytest.mark.asyncio
async def test_ignores_already_resolved_rows(peer: str) -> None:
    await _insert(
        peer=peer,
        page_id="<page_a>",
        signal_timestamp=1,
        sent_ago=timedelta(hours=1),
        expires_in=timedelta(hours=23),
        awaiting_reply=False,
    )

    assert await load_awaiting_reply(peer) is None


@pytest.mark.asyncio
async def test_returns_most_recent_of_several(peer: str) -> None:
    await _insert(
        peer=peer,
        page_id="<page_old>",
        signal_timestamp=1,
        sent_ago=timedelta(hours=10),
        expires_in=timedelta(hours=14),
    )
    await _insert(
        peer=peer,
        page_id="<page_new>",
        signal_timestamp=2,
        sent_ago=timedelta(hours=1),
        expires_in=timedelta(hours=23),
    )

    entry = await load_awaiting_reply(peer)

    assert entry is not None
    assert entry["notion_page_id"] == "<page_new>"


@pytest.mark.asyncio
async def test_does_not_leak_across_peers(peer: str) -> None:
    other = f"<test-{uuid.uuid4()}>"
    await _insert(
        peer=other,
        page_id="<page_a>",
        signal_timestamp=1,
        sent_ago=timedelta(hours=1),
        expires_in=timedelta(hours=23),
    )
    try:
        assert await load_awaiting_reply(peer) is None
    finally:
        from app.tools.db import get_db_conn

        async with get_db_conn() as conn:
            await conn.execute("DELETE FROM recent_outbound WHERE peer = %s", (other,))


@pytest.mark.asyncio
async def test_clear_makes_the_row_unmatchable(peer: str) -> None:
    """A resolved reminder must not match a second reply."""
    await _insert(
        peer=peer,
        page_id="<page_a>",
        signal_timestamp=7,
        sent_ago=timedelta(hours=1),
        expires_in=timedelta(hours=23),
    )

    assert await clear_awaiting_reply(peer=peer, signal_timestamp=7) is True
    assert await load_awaiting_reply(peer) is None
    # Idempotent: clearing again reports no match rather than raising.
    assert await clear_awaiting_reply(peer=peer, signal_timestamp=7) is False
