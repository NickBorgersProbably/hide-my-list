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

This module is the only authorised place that imports httpx.AsyncClient
for Notion calls. All requests go through _client() to allow test injection.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import httpx

NOTION_VERSION = "2022-06-28"
API_BASE = "https://api.notion.com/v1"


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
) -> dict[str, Any]:
    """Create a task page in the Notion database.

    Mirrors: notion-cli.sh create-task
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
        raise ValueError(
            f"reminder_status must be 'sent' or 'missed', got {reminder_status!r}"
        )

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
