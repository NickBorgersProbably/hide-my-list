"""COMPLETE node: task completion + reward integration.

Marks the active task as completed in Notion, triggers the reward subsystem,
and drafts a celebration message into pending_outbound.

Reward integration implemented in PR-B5.
"""
from __future__ import annotations

from typing import Any

import structlog

from app.graph.state import OutboundDraft, State

log = structlog.get_logger(__name__)

async def complete_node(state: State) -> dict[str, Any]:
    """COMPLETE handler: update Notion, call rewards.maybe_reward(), draft reply."""
    peer = state.get("peer", "")

    try:
        from app.tools import notion
        from app.tools.rewards import maybe_reward

        active_task = state.get("active_task")
        if not active_task:
            # No active task — may be a reminder completion via recent_outbound
            no_task_draft: OutboundDraft = {
                "recipient": peer,
                "body": "Done! Nice work. Want another task?",
                "notion_page_id": None,
            }
            return {"pending_outbound": [no_task_draft], "conversation_state": "idle"}

        page_id = active_task.get("page_id", "")
        task_title = active_task.get("title", "task")

        # Mark task completed in Notion
        if page_id:
            await notion.update_status(page_id, "Completed")

        # Calculate streak / completion count
        streak = state.get("streak", 0) + 1
        tasks_today = state.get("tasks_completed_today", 0) + 1

        # Get reward result — text celebration + optional image path
        reward_result = await maybe_reward(
            peer=peer,
            task_title=task_title,
            notion_page_id=page_id,
            streak=streak,
            work_type=active_task.get("work_type", ""),
            energy_required=active_task.get("energy_required", ""),
        )

        reward_draft: OutboundDraft = {
            "recipient": peer,
            "body": reward_result["text"],
            "notion_page_id": page_id,
        }
        # Attach image if one was generated.
        # attachment_path is private; never log the path value.
        if reward_result["attachment_path"]:
            reward_draft["attachment_path"] = reward_result["attachment_path"]

        log.info("complete_node.done", peer=peer, page_id=page_id, streak=streak)
        return {
            "pending_outbound": [reward_draft],
            "active_task": None,
            "streak": streak,
            "tasks_completed_today": tasks_today,
            "conversation_state": "idle",
        }

    except Exception:
        log.exception("complete_node.error", peer=peer)
        fallback: OutboundDraft = {
            "recipient": peer,
            "body": "Got it, marked done! Nice work.",
            "notion_page_id": None,
        }
        return {
            "pending_outbound": [fallback],
            "active_task": None,
            "conversation_state": "idle",
        }
