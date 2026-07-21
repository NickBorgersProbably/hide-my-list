"""Tests for Notion database schema health validation."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

import app.tools.notion as notion_module


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _Client:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.paths: list[str] = []

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get(self, path: str) -> _Response:
        self.paths.append(path)
        return _Response(self.payload)


def _database_payload(**overrides: str | None) -> dict[str, Any]:
    properties = {
        name: {"type": prop_type}
        for name, prop_type in notion_module.REQUIRED_DATABASE_PROPERTIES.items()
    }
    for name, prop_type in overrides.items():
        if prop_type is None:
            properties.pop(name)
        else:
            properties[name] = {"type": prop_type}
    return {"object": "database", "properties": properties}


@pytest.mark.asyncio
async def test_verify_database_schema_accepts_complete_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database with every required property and expected type passes."""
    monkeypatch.setenv("NOTION_DATABASE_ID", "test-database-id")
    client = _Client(_database_payload())
    monkeypatch.setattr(notion_module, "_client_factory", lambda: client)

    result = await notion_module.verify_database_schema()

    assert result.ok is True
    assert result.detail is None
    assert client.paths == ["/databases/test-database-id"]


@pytest.mark.asyncio
async def test_verify_database_schema_reports_missing_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing property is named in the schema result and detail."""
    monkeypatch.setenv("NOTION_DATABASE_ID", "test-database-id")
    client = _Client(
        _database_payload(
            **{
                "Due At": None,
                "Reminder Scheduled At": None,
            }
        )
    )
    monkeypatch.setattr(notion_module, "_client_factory", lambda: client)

    result = await notion_module.verify_database_schema()

    assert result.ok is False
    assert result.missing_properties == ("Due At", "Reminder Scheduled At")
    assert result.detail is not None
    assert "Due At (date)" in result.detail
    assert "Reminder Scheduled At (date)" in result.detail


@pytest.mark.asyncio
async def test_verify_database_schema_reports_type_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A property with the wrong Notion type is named with expected and actual types."""
    monkeypatch.setenv("NOTION_DATABASE_ID", "test-database-id")
    client = _Client(_database_payload(Urgency="rich_text"))
    monkeypatch.setattr(notion_module, "_client_factory", lambda: client)

    result = await notion_module.verify_database_schema()

    assert result.ok is False
    assert result.type_mismatches == (
        notion_module.SchemaTypeMismatch(
            property_name="Urgency",
            expected_type="number",
            actual_type="rich_text",
        ),
    )
    assert result.detail is not None
    assert "Urgency expected number got rich_text" in result.detail


@pytest.mark.asyncio
async def test_check_notion_health_enqueues_schema_alert_with_property_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scheduled health job alerts on schema drift with the concrete failures."""
    from app.scheduler.jobs import check_notion_health

    enqueued: list[dict[str, str]] = []

    async def enqueue(**kwargs: str) -> None:
        enqueued.append(kwargs)

    monkeypatch.setattr(
        "app.tools.notion.health_check",
        AsyncMock(return_value=notion_module.HealthCheckResult(ok=True)),
    )
    monkeypatch.setattr(
        "app.tools.notion.verify_database_schema",
        AsyncMock(
            return_value=notion_module.SchemaCheckResult(
                ok=False,
                detail=(
                    "missing properties: Due At (date); type mismatches: "
                    "Urgency expected number got rich_text"
                ),
                missing_properties=("Due At",),
                type_mismatches=(
                    notion_module.SchemaTypeMismatch(
                        property_name="Urgency",
                        expected_type="number",
                        actual_type="rich_text",
                    ),
                ),
            )
        ),
    )
    monkeypatch.setattr("app.tools.ops_alerts.enqueue", enqueue)

    await check_notion_health()

    assert enqueued == [
        {
            "kind": "notion_schema_mismatch",
            "body": (
                "Notion database schema check failed: "
                "missing properties: Due At (date); type mismatches: "
                "Urgency expected number got rich_text"
            ),
            "severity": "critical",
        }
    ]


@pytest.mark.asyncio
async def test_check_notion_health_skips_schema_when_connectivity_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed connectivity probe alerts and avoids a database schema request."""
    from app.scheduler.jobs import check_notion_health

    enqueued: list[dict[str, str]] = []
    schema_check = AsyncMock(return_value=notion_module.SchemaCheckResult(ok=True))

    async def enqueue(**kwargs: str) -> None:
        enqueued.append(kwargs)

    monkeypatch.setattr(
        "app.tools.notion.health_check",
        AsyncMock(
            return_value=notion_module.HealthCheckResult(
                ok=False,
                detail="HTTPStatusError: 401 Unauthorized",
            )
        ),
    )
    monkeypatch.setattr("app.tools.notion.verify_database_schema", schema_check)
    monkeypatch.setattr("app.tools.ops_alerts.enqueue", enqueue)

    await check_notion_health()

    schema_check.assert_not_awaited()
    assert enqueued == [
        {
            "kind": "notion_health_failed",
            "body": "Notion API health check failed: HTTPStatusError: 401 Unauthorized",
            "severity": "warning",
        }
    ]
