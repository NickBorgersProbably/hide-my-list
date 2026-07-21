"""Regression coverage for reward image generation with empty task titles."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeModel:
    def __init__(self, content: str) -> None:
        self._content = content

    async def ainvoke(self, messages: object) -> _FakeResponse:
        return _FakeResponse(self._content)


def _state(*, intent: str, incoming: str) -> dict[str, Any]:
    return {
        "peer": "<recipient>",
        "incoming": incoming,
        "intent": intent,
        "messages": [],
        "active_task": None,
        "streak": 0,
        "tasks_completed_today": 0,
        "user_prefs": {},
        "mood": None,
        "available_minutes": 30,
        "conversation_state": "idle",
        "pending_outbound": [],
    }


def _notion_task(page_id: str, title: str) -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Work Type": {"select": {"name": "focus"}},
            "Time Estimate (min)": {"number": 30},
            "Urgency": {"number": 50},
            "Energy Required": {"select": {"name": "Medium"}},
            "Rejection Count": {"number": 0},
        },
    }


@pytest.mark.asyncio
async def test_intake_rejects_blank_model_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank model title falls back to the user's captured task text."""
    from app import models as models_module
    from app.graph.nodes.intake import intake_node
    from app.tools import notion

    created: dict[str, Any] = {}
    user_message = "Placeholder captured task"
    model_response = json.dumps(
        {
            "action": "save",
            "title": "   ",
            "work_type": "focus",
            "urgency": 50,
            "time_estimate_minutes": 30,
            "energy_required": "Medium",
            "is_reminder": False,
            "remind_at": None,
            "due_at": None,
            "use_hidden_subtasks": False,
            "sub_tasks": [],
            "inline_steps": "",
            "confirmation_message": "Added.",
        }
    )

    async def fake_query_all() -> dict[str, Any]:
        return {"results": []}

    async def fake_create_task(**kwargs: Any) -> dict[str, Any]:
        created.update(kwargs)
        return {"id": "<page-id-created>"}

    monkeypatch.setattr(models_module, "llm", lambda tier, **kwargs: _FakeModel(model_response))
    monkeypatch.setattr(notion, "query_all", fake_query_all)
    monkeypatch.setattr(notion, "create_task", fake_create_task)

    result = await intake_node(_state(intent="ADD_TASK", incoming=user_message))

    assert created["title"] == user_message
    assert result["pending_outbound"][0]["notion_page_title"] == user_message


@pytest.mark.asyncio
async def test_selection_skips_blank_title_without_sending_task_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selection does not start a blank-title task or send an unresolved token."""
    from app import models as models_module
    from app.graph.nodes.selection import selection_node
    from app.tools import notion

    page_id = "<page-id-selected>"
    model_response = json.dumps(
        {
            "selected_task_id": page_id,
            "score": 0.9,
            "reasoning": "fits time",
            "user_message": "How about {task}?",
        }
    )

    async def fake_query_pending() -> dict[str, Any]:
        return {"results": [_notion_task(page_id, "   ")]}

    async def fake_update_status(pid: str, status: str) -> dict[str, Any]:
        return {"id": pid, "status": status}

    monkeypatch.setattr(models_module, "llm", lambda tier, **kwargs: _FakeModel(model_response))
    monkeypatch.setattr(notion, "query_pending", fake_query_pending)
    monkeypatch.setattr(notion, "update_status", fake_update_status)

    result = await selection_node(_state(intent="GET_TASK", incoming="what should I do?"))

    draft = result["pending_outbound"][0]
    assert result["active_task"] is None
    assert result["conversation_state"] == "selection"
    assert draft["notion_page_id"] is None
    assert "{task}" not in draft["body"]


@pytest.mark.asyncio
async def test_complete_passes_fallback_title_to_rewards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Complete treats a blank active_task title as absent before rewarding."""
    from app.graph.nodes.complete import complete_node
    from app.tools import notion
    from app.tools import rewards as rewards_module

    maybe_reward = AsyncMock(return_value={"text": "Nice work!", "attachment_path": None})
    monkeypatch.setattr(notion, "update_status", AsyncMock())
    monkeypatch.setattr(rewards_module, "maybe_reward", maybe_reward)

    state = _state(intent="COMPLETE", incoming="done")
    state["active_task"] = {
        "page_id": "<page-id-active>",
        "title": "   ",
        "status": "In Progress",
        "work_type": "focus",
        "urgency": 50,
        "time_estimate": 30,
        "energy_required": "Medium",
    }

    await complete_node(state)

    assert maybe_reward.await_args.kwargs["task_title"] == "task"


@pytest.mark.asyncio
async def test_generate_reward_image_allows_blank_descriptions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Blank descriptions do not block image generation."""
    from app.tools.rewards import generate_reward_image

    fake_image = MagicMock()
    fake_image.b64_json = base64.b64encode(b"fake-image-bytes").decode()
    fake_response = MagicMock()
    fake_response.data = [fake_image]

    generate = AsyncMock(return_value=fake_response)
    client = MagicMock()
    client.images = MagicMock()
    client.images.generate = generate

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("REWARD_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setattr("openai.AsyncOpenAI", MagicMock(return_value=client))

    result = await generate_reward_image(
        intensity="medium",
        streak_count=1,
        task_descriptions=["   "],
    )

    assert result is not None
    generate.assert_awaited_once()
