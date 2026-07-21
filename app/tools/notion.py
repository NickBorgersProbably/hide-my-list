"""Notion API client for hide-my-list.

Covers all 9 verbs from scripts/notion-cli.sh:
  1. create_task
  2. create_reminder
  3. query_pending
  4. query_all
  5. query_due_reminders
  6. update_status
  7. complete_reminder
  8. update_property
  9. get_page

Plus deadline-reminder plumbing verbs:
  10. query_tasks_with_unscheduled_deadlines
  11. query_scheduled_tasks_with_deadlines
  12. mark_reminder_scheduled

Plus health helpers used by notion_health:
  health_check()
  verify_database_schema()

This module is the only authorised place that imports httpx.AsyncClient
for Notion calls. All requests go through _client() to allow test injection.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any, NamedTuple

import httpx

NOTION_VERSION = "2022-06-28"
API_BASE = "https://api.notion.com/v1"

REQUIRED_DATABASE_PROPERTIES: dict[str, str] = {
    "Title": "title",
    "Status": "select",
    "Work Type": "select",
    "Urgency": "number",
    "Time Estimate (min)": "number",
    "Energy Required": "select",
    "Created At": "date",
    "Completed At": "date",
    "Rejection Count": "number",
    "Rejection Notes": "rich_text",
    "AI Context": "rich_text",
    "Steps Completed": "number",
    "Resume Count": "number",
    "Last Resumed At": "date",
    "Inline Steps": "rich_text",
    "Parent Task": "relation",
    "Sequence": "number",
    "Progress Notes": "rich_text",
    "Due At": "date",
    "Is Reminder": "checkbox",
    "Remind At": "date",
    "Reminder Status": "select",
    "Started At": "date",
    "Reminder Scheduled At": "date",
}


def _default_client() -> httpx.AsyncClient:
    api_key = os.environ["NOTION_API_KEY"]
    return httpx.AsyncClient(
        base_url=API_BASE,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0),
    )


# Module-level client factory — replaced in tests via monkeypatching
_client_factory = _default_client


def _database_id() -> str:
    return os.environ["NOTION_DATABASE_ID"]


# ---------------------------------------------------------------------------
# Verb 1 — create_task
# ---------------------------------------------------------------------------


async def create_task(
    title: str,
    work_type: str,
    urgency: int = 50,
    time_estimate: int = 30,
    energy_required: str = "Medium",
    inline_steps: str = "",
    status: str = "Pending",
    parent_id: str = "",
    sequence: int | None = None,
    due_at_iso: str | None = None,
) -> dict[str, Any]:
    """Create a task page in the Notion database.

    Mirrors: notion-cli.sh create-task

    Args:
        due_at_iso: Optional ISO 8601 deadline string (e.g. "2026-06-03T17:00:00-05:00").
            When set, the "Due At" date property is included in the payload.
            When None (default), the field is omitted entirely — Notion treats it as empty.
    """
    props: dict[str, Any] = {
        "Title": {"title": [{"text": {"content": title}}]},
        "Status": {"select": {"name": status}},
        "Work Type": {"select": {"name": work_type}},
        "Urgency": {"number": urgency},
        "Time Estimate (min)": {"number": time_estimate},
        "Energy Required": {"select": {"name": energy_required}},
        "Rejection Count": {"number": 0},
        "Steps Completed": {"number": 0},
        "Resume Count": {"number": 0},
    }
    if inline_steps:
        props["Inline Steps"] = {"rich_text": [{"text": {"content": inline_steps}}]}
    if parent_id:
        props["Parent Task"] = {"relation": [{"id": parent_id}]}
    if sequence is not None:
        props["Sequence"] = {"number": sequence}
    if due_at_iso is not None:
        props["Due At"] = {"date": {"start": due_at_iso}}

    payload = {
        "parent": {"database_id": _database_id()},
        "properties": props,
    }
    async with _client_factory() as client:
        resp = await client.post("/pages", json=payload)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Verb 2 — create_reminder
# ---------------------------------------------------------------------------


async def create_reminder(
    title: str,
    remind_at_iso: str,
    work_type: str = "Independent",
    energy_required: str = "Low",
) -> dict[str, Any]:
    """Create a reminder page in the Notion database.

    Mirrors: notion-cli.sh create-reminder
    """
    props: dict[str, Any] = {
        "Title": {"title": [{"text": {"content": title}}]},
        "Status": {"select": {"name": "Pending"}},
        "Work Type": {"select": {"name": work_type}},
        "Urgency": {"number": 90},
        "Time Estimate (min)": {"number": 5},
        "Energy Required": {"select": {"name": energy_required}},
        "Rejection Count": {"number": 0},
        "Steps Completed": {"number": 0},
        "Resume Count": {"number": 0},
        "Is Reminder": {"checkbox": True},
        "Remind At": {"date": {"start": remind_at_iso}},
        "Reminder Status": {"select": {"name": "pending"}},
    }
    payload = {
        "parent": {"database_id": _database_id()},
        "properties": props,
    }
    async with _client_factory() as client:
        resp = await client.post("/pages", json=payload)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Verb 3 — query_pending
# ---------------------------------------------------------------------------


async def query_pending() -> dict[str, Any]:
    """Query all pending (non-reminder) tasks, sorted by urgency descending.

    Mirrors: notion-cli.sh query-pending
    """
    payload = {
        "filter": {
            "and": [
                {"property": "Status", "select": {"equals": "Pending"}},
                {"property": "Is Reminder", "checkbox": {"equals": False}},
            ]
        },
        "sorts": [{"property": "Urgency", "direction": "descending"}],
    }
    async with _client_factory() as client:
        resp = await client.post(f"/databases/{_database_id()}/query", json=payload)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Verb 4 — query_all
# ---------------------------------------------------------------------------


async def query_all() -> dict[str, Any]:
    """Query all tasks, sorted by urgency descending.

    Mirrors: notion-cli.sh query-all
    """
    payload = {
        "sorts": [{"property": "Urgency", "direction": "descending"}],
    }
    async with _client_factory() as client:
        resp = await client.post(f"/databases/{_database_id()}/query", json=payload)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Verb 5 — query_due_reminders
# ---------------------------------------------------------------------------


async def query_due_reminders(before_iso: str | None = None) -> dict[str, Any]:
    """Query reminders due on or before the given ISO timestamp.

    If before_iso is None, defaults to the current UTC time.

    Mirrors: notion-cli.sh query-due-reminders
    """
    if before_iso is None:
        before_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    payload = {
        "filter": {
            "and": [
                {"property": "Is Reminder", "checkbox": {"equals": True}},
                {"property": "Reminder Status", "select": {"equals": "pending"}},
                {"property": "Status", "select": {"equals": "Pending"}},
                {"property": "Remind At", "date": {"on_or_before": before_iso}},
            ]
        },
        "sorts": [{"property": "Remind At", "direction": "ascending"}],
    }
    async with _client_factory() as client:
        resp = await client.post(f"/databases/{_database_id()}/query", json=payload)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Verb 6 — update_status
# ---------------------------------------------------------------------------


async def update_status(page_id: str, new_status: str) -> dict[str, Any]:
    """Update the Status property of a page.

    Sets Completed At when transitioning to Completed.
    Sets Started At when transitioning to In Progress (only if not already set).

    Mirrors: notion-cli.sh update-status
    """
    # Fetch current page to check Started At
    async with _client_factory() as client:
        page_resp = await client.get(f"/pages/{page_id}")
        page_resp.raise_for_status()
        page = page_resp.json()

    props: dict[str, Any] = {"Status": {"select": {"name": new_status}}}
    now = datetime.now(UTC).isoformat()

    if new_status == "Completed":
        props["Completed At"] = {"date": {"start": now}}
    elif new_status == "In Progress":
        started_at = None
        started_prop = page.get("properties", {}).get("Started At")
        if isinstance(started_prop, dict):
            date_value = started_prop.get("date")
            if isinstance(date_value, dict):
                started_at = date_value.get("start")
        if not started_at:
            props["Started At"] = {"date": {"start": now}}

    async with _client_factory() as client:
        resp = await client.patch(f"/pages/{page_id}", json={"properties": props})
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Verb 7 — complete_reminder
# ---------------------------------------------------------------------------


async def complete_reminder(
    page_id: str,
    reminder_status: str,
) -> dict[str, Any]:
    """Mark a reminder as complete with the given reminder_status.

    reminder_status must be "sent" or "missed".

    Mirrors: notion-cli.sh complete-reminder
    """
    if reminder_status not in ("sent", "missed"):
        raise ValueError(f"reminder_status must be 'sent' or 'missed', got {reminder_status!r}")

    now = datetime.now(UTC).isoformat()
    props: dict[str, Any] = {
        "Status": {"select": {"name": "Completed"}},
        "Reminder Status": {"select": {"name": reminder_status}},
        "Completed At": {"date": {"start": now}},
    }
    async with _client_factory() as client:
        resp = await client.patch(f"/pages/{page_id}", json={"properties": props})
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Verb 8 — update_property
# ---------------------------------------------------------------------------


async def update_property(page_id: str, prop_json: dict[str, Any]) -> dict[str, Any]:
    """Apply an arbitrary properties patch to a page.

    Mirrors: notion-cli.sh update-property
    """
    async with _client_factory() as client:
        resp = await client.patch(f"/pages/{page_id}", json=prop_json)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Verb 9 — get_page
# ---------------------------------------------------------------------------


async def get_page(page_id: str) -> dict[str, Any]:
    """Fetch a single page by ID.

    Mirrors: notion-cli.sh get-page
    """
    async with _client_factory() as client:
        resp = await client.get(f"/pages/{page_id}")
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Verb 10 — query_tasks_with_unscheduled_deadlines
# ---------------------------------------------------------------------------


async def query_tasks_with_unscheduled_deadlines() -> dict[str, Any]:
    """Return tasks with a Due At but no Reminder Scheduled At yet.

    4-condition AND filter:
      - Due At is_not_empty
      - Reminder Scheduled At is_empty
      - Status != Completed
      - Is Reminder = false

    Sorted by Due At ascending (soonest deadline first).
    """
    payload = {
        "filter": {
            "and": [
                {"property": "Due At", "date": {"is_not_empty": True}},
                {"property": "Reminder Scheduled At", "date": {"is_empty": True}},
                {"property": "Status", "select": {"does_not_equal": "Completed"}},
                {"property": "Is Reminder", "checkbox": {"equals": False}},
            ]
        },
        "sorts": [{"property": "Due At", "direction": "ascending"}],
    }
    async with _client_factory() as client:
        resp = await client.post(f"/databases/{_database_id()}/query", json=payload)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Verb 11 — query_scheduled_tasks_with_deadlines
# ---------------------------------------------------------------------------


async def query_scheduled_tasks_with_deadlines() -> dict[str, Any]:
    """Return active non-reminder tasks whose deadline series was scheduled.

    The nightly daemon uses this second query to detect edits to Due At. The
    orphan query intentionally excludes these rows, so edit detection cannot
    rely on it.
    """
    payload = {
        "filter": {
            "and": [
                {"property": "Due At", "date": {"is_not_empty": True}},
                {"property": "Reminder Scheduled At", "date": {"is_not_empty": True}},
                {"property": "Status", "select": {"does_not_equal": "Completed"}},
                {"property": "Is Reminder", "checkbox": {"equals": False}},
            ]
        },
        "sorts": [{"property": "Due At", "direction": "ascending"}],
    }
    async with _client_factory() as client:
        resp = await client.post(f"/databases/{_database_id()}/query", json=payload)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Verb 12 — mark_reminder_scheduled
# ---------------------------------------------------------------------------


async def mark_reminder_scheduled(page_id: str) -> dict[str, Any]:
    """Set Reminder Scheduled At to UTC now, marking the reminder series as created.

    Idempotency guard: query_tasks_with_unscheduled_deadlines() excludes tasks
    where this field is set.
    """
    now = datetime.now(UTC).isoformat()
    props: dict[str, Any] = {
        "Reminder Scheduled At": {"date": {"start": now}},
    }
    async with _client_factory() as client:
        resp = await client.patch(f"/pages/{page_id}", json={"properties": props})
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Health check — used by notion_health APScheduler job
# ---------------------------------------------------------------------------


#: Cap on the failure detail carried into an ops alert. The probe hits
#: /users/me, so an exception string holds an HTTP status, a Notion API URL,
#: or a transport error — never task content. The cap bounds a pathological
#: exception repr rather than guarding privacy.
_HEALTH_DETAIL_MAX_CHARS = 200


class HealthCheckResult(NamedTuple):
    """Outcome of a Notion connectivity probe.

    `detail` carries the failure reason so the caller can surface it in an
    ops alert. Without it the operator receives "check failed, verify your
    key or reachability" — two unrelated causes and no way to tell them
    apart once the logs have rotated or gone unreachable.
    """

    ok: bool
    detail: str | None = None


class SchemaTypeMismatch(NamedTuple):
    """A Notion database property exists with an unexpected type."""

    property_name: str
    expected_type: str
    actual_type: str


class SchemaCheckResult(NamedTuple):
    """Outcome of validating the task database properties used by the app."""

    ok: bool
    detail: str | None = None
    missing_properties: tuple[str, ...] = ()
    type_mismatches: tuple[SchemaTypeMismatch, ...] = ()


def _format_schema_detail(
    missing_properties: tuple[str, ...],
    type_mismatches: tuple[SchemaTypeMismatch, ...],
) -> str:
    parts: list[str] = []
    if missing_properties:
        parts.append(
            "missing properties: "
            + ", ".join(
                f"{name} ({REQUIRED_DATABASE_PROPERTIES[name]})"
                for name in missing_properties
            )
        )
    if type_mismatches:
        parts.append(
            "type mismatches: "
            + ", ".join(
                f"{item.property_name} expected {item.expected_type} got {item.actual_type}"
                for item in type_mismatches
            )
        )
    return "; ".join(parts)


async def verify_database_schema() -> SchemaCheckResult:
    """Validate that the configured task database has the properties the app uses.

    The health job calls this after the connectivity probe. It catches schema
    drift within the 15-minute health cadence instead of letting the next verb
    that touches the missing property fail with an opaque Notion 400.
    """
    import structlog

    _log = structlog.get_logger(__name__)

    try:
        async with _client_factory() as client:
            resp = await client.get(f"/databases/{_database_id()}")
            resp.raise_for_status()
        database = resp.json()
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"[:_HEALTH_DETAIL_MAX_CHARS]
        _log.error("notion.schema_check.failed", error=detail)
        return SchemaCheckResult(ok=False, detail=detail)

    raw_properties = database.get("properties", {})
    properties = raw_properties if isinstance(raw_properties, dict) else {}

    missing = tuple(
        name
        for name in REQUIRED_DATABASE_PROPERTIES
        if name not in properties
    )
    mismatches: list[SchemaTypeMismatch] = []
    for name, expected_type in REQUIRED_DATABASE_PROPERTIES.items():
        prop = properties.get(name)
        if not isinstance(prop, dict):
            continue
        actual_type = prop.get("type")
        if actual_type != expected_type:
            mismatches.append(
                SchemaTypeMismatch(
                    property_name=name,
                    expected_type=expected_type,
                    actual_type=str(actual_type),
                )
            )

    type_mismatches = tuple(mismatches)
    if missing or type_mismatches:
        detail = _format_schema_detail(missing, type_mismatches)
        _log.error(
            "notion.schema_check.mismatch",
            missing_properties=list(missing),
            type_mismatches=[
                {
                    "property_name": item.property_name,
                    "expected_type": item.expected_type,
                    "actual_type": item.actual_type,
                }
                for item in type_mismatches
            ],
        )
        return SchemaCheckResult(
            ok=False,
            detail=detail,
            missing_properties=missing,
            type_mismatches=type_mismatches,
        )

    _log.info("notion.schema_check.ok", property_count=len(REQUIRED_DATABASE_PROPERTIES))
    return SchemaCheckResult(ok=True)


async def health_check() -> HealthCheckResult:
    """Probe Notion API connectivity via GET /v1/users/me.

    Returns `HealthCheckResult(ok=True)` on success, or `ok=False` with the
    failure reason in `detail`. Raises nothing — designed for scheduler job
    use where exceptions would crash the job rather than mark it failed.

    Returns a result object rather than a bare bool so the reason survives
    into the alert. Callers must branch on `.ok`: the result itself is a
    tuple and therefore always truthy.
    """
    import structlog

    _log = structlog.get_logger(__name__)

    try:
        async with _client_factory() as client:
            resp = await client.get("/users/me")
            resp.raise_for_status()
        _log.info("notion.health_check.ok", status=resp.status_code)
        return HealthCheckResult(ok=True)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"[:_HEALTH_DETAIL_MAX_CHARS]
        _log.error("notion.health_check.failed", error=detail)
        return HealthCheckResult(ok=False, detail=detail)
