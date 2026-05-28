"""Integration tests for Due At + Reminder Scheduled At plumbing in notion.py.

Tests assert HTTP request shape for the two new verbs:
  - query_tasks_with_unscheduled_deadlines()
  - mark_reminder_scheduled(page_id)

Uses pytest-httpserver to capture actual HTTP traffic from the Python client.
No real Notion account or DATABASE_URL is required.

Private data discipline: no real page IDs, titles, or recipient data.
All test values use placeholder strings.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from pytest_httpserver import HTTPServer

import app.tools.notion as notion_module

# ---------------------------------------------------------------------------
# Fixtures (mirrors test_notion_parity.py pattern)
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_page_id() -> str:
    return str(uuid.uuid4()).replace("-", "")


@pytest.fixture()
def fake_db_id() -> str:
    return str(uuid.uuid4()).replace("-", "")


@pytest.fixture()
def notion_server(
    httpserver: HTTPServer,
    fake_db_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> HTTPServer:
    """Configure environment and redirect httpx to the test server."""
    base_url = httpserver.url_for("/").rstrip("/")

    monkeypatch.setenv("NOTION_API_KEY", "test-api-key")
    monkeypatch.setenv("NOTION_DATABASE_ID", fake_db_id)

    import httpx

    def _test_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": "Bearer test-api-key",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0),
        )

    monkeypatch.setattr(notion_module, "_client_factory", _test_client)
    return httpserver


def _captured_body(request_data: bytes) -> dict:  # type: ignore[type-arg]
    """Parse JSON body from captured request bytes."""
    return json.loads(request_data.decode("utf-8"))  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# query_tasks_with_unscheduled_deadlines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_tasks_with_unscheduled_deadlines_filter_shape(
    notion_server: HTTPServer,
    fake_db_id: str,
) -> None:
    """query_tasks_with_unscheduled_deadlines sends POST with 4 ANDed filter conditions."""
    notion_server.expect_request(f"/databases/{fake_db_id}/query", method="POST").respond_with_json(
        {"results": []}
    )

    await notion_module.query_tasks_with_unscheduled_deadlines()

    assert len(notion_server.log) == 1
    req = notion_server.log[0][0]
    body = _captured_body(req.data)

    assert req.method == "POST"
    assert req.path == f"/databases/{fake_db_id}/query"

    filters = body["filter"]["and"]
    assert len(filters) == 4

    # Condition 1: Due At is_not_empty
    due_at_filter = next(f for f in filters if f.get("property") == "Due At")
    assert due_at_filter["date"]["is_not_empty"] is True

    # Condition 2: Reminder Scheduled At is_empty
    rsa_filter = next(f for f in filters if f.get("property") == "Reminder Scheduled At")
    assert rsa_filter["date"]["is_empty"] is True

    # Condition 3: Status != "Completed"
    status_filter = next(f for f in filters if f.get("property") == "Status")
    assert status_filter["select"]["does_not_equal"] == "Completed"

    # Condition 4: Is Reminder = false
    is_reminder_filter = next(f for f in filters if f.get("property") == "Is Reminder")
    assert is_reminder_filter["checkbox"]["equals"] is False


@pytest.mark.asyncio
async def test_query_tasks_with_unscheduled_deadlines_sort(
    notion_server: HTTPServer,
    fake_db_id: str,
) -> None:
    """query_tasks_with_unscheduled_deadlines sorts by Due At ascending."""
    notion_server.expect_request(f"/databases/{fake_db_id}/query", method="POST").respond_with_json(
        {"results": []}
    )

    await notion_module.query_tasks_with_unscheduled_deadlines()

    req = notion_server.log[0][0]
    body = _captured_body(req.data)

    assert len(body["sorts"]) == 1
    assert body["sorts"][0]["property"] == "Due At"
    assert body["sorts"][0]["direction"] == "ascending"


@pytest.mark.asyncio
async def test_query_tasks_with_unscheduled_deadlines_returns_results(
    notion_server: HTTPServer,
    fake_db_id: str,
) -> None:
    """query_tasks_with_unscheduled_deadlines returns the full Notion response dict."""
    fake_results = {
        "results": [
            {"object": "page", "id": "fake-page-id-1"},
            {"object": "page", "id": "fake-page-id-2"},
        ]
    }
    notion_server.expect_request(f"/databases/{fake_db_id}/query", method="POST").respond_with_json(
        fake_results
    )

    result = await notion_module.query_tasks_with_unscheduled_deadlines()

    assert len(result["results"]) == 2
    assert result["results"][0]["id"] == "fake-page-id-1"


# ---------------------------------------------------------------------------
# mark_reminder_scheduled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_reminder_scheduled_sends_patch(
    notion_server: HTTPServer,
    fake_page_id: str,
) -> None:
    """mark_reminder_scheduled sends PATCH /pages/{page_id} with Reminder Scheduled At."""
    notion_server.expect_request(f"/pages/{fake_page_id}", method="PATCH").respond_with_json(
        {"object": "page", "id": fake_page_id}
    )

    before = datetime.now(UTC)
    await notion_module.mark_reminder_scheduled(fake_page_id)

    assert len(notion_server.log) == 1
    req = notion_server.log[0][0]
    body = _captured_body(req.data)

    assert req.method == "PATCH"
    assert req.path == f"/pages/{fake_page_id}"

    props = body["properties"]
    assert "Reminder Scheduled At" in props
    rsa_start = props["Reminder Scheduled At"]["date"]["start"]

    # Verify it's a parseable ISO 8601 UTC timestamp close to now
    parsed = datetime.fromisoformat(rsa_start)
    assert abs((parsed - before).total_seconds()) <= 5


@pytest.mark.asyncio
async def test_mark_reminder_scheduled_timestamp_is_utc(
    notion_server: HTTPServer,
    fake_page_id: str,
) -> None:
    """mark_reminder_scheduled sets Reminder Scheduled At to current UTC time."""
    notion_server.expect_request(f"/pages/{fake_page_id}", method="PATCH").respond_with_json(
        {"object": "page", "id": fake_page_id}
    )

    before = datetime.now(UTC)
    await notion_module.mark_reminder_scheduled(fake_page_id)

    req = notion_server.log[0][0]
    body = _captured_body(req.data)
    rsa_start = body["properties"]["Reminder Scheduled At"]["date"]["start"]

    # datetime.isoformat() on UTC-aware datetime produces an offset-aware string
    parsed = datetime.fromisoformat(rsa_start)
    assert parsed.tzinfo is not None
    # Must be no earlier than before the call
    assert parsed >= before.replace(microsecond=0)


@pytest.mark.asyncio
async def test_mark_reminder_scheduled_returns_page(
    notion_server: HTTPServer,
    fake_page_id: str,
) -> None:
    """mark_reminder_scheduled returns the updated page object from Notion."""
    notion_server.expect_request(f"/pages/{fake_page_id}", method="PATCH").respond_with_json(
        {"object": "page", "id": fake_page_id, "updated": True}
    )

    result = await notion_module.mark_reminder_scheduled(fake_page_id)

    assert result["id"] == fake_page_id
    assert result["updated"] is True
