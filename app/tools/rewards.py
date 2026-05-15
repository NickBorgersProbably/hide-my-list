"""Reward subsystem for hide-my-list.

v1 scope: emoji + image rewards (docs/reward-system.md).
Audio rewards and outing suggestions deferred to v1.1.

Full implementation in PR-B5. This stub provides the interface
used by the COMPLETE node, returning a basic celebration message.

Private data discipline:
- task_title is NEVER logged (it is private user data)
- reward_manifests table stores task_title in Postgres only (never stdout)
- Generated images stored under the reward_artifacts volume mount
"""
from __future__ import annotations

import os
from typing import Any

import structlog

log = structlog.get_logger(__name__)


async def maybe_reward(
    *,
    peer: str,
    task_title: str,
    notion_page_id: str,
    streak: int,
    work_type: str,
    energy_required: str,
) -> str:
    """Generate a reward message for a task completion.

    Returns a celebration string to include in pending_outbound.
    Full reward subsystem (image gen, feedback loop, manifests) implemented in PR-B5.

    Args:
        peer: E.164 sender phone number.
        task_title: Title of the completed task (private — never logged).
        notion_page_id: Notion page ID of the completed task.
        streak: Current consecutive completion streak count.
        work_type: Task work type (focus/creative/social/independent).
        energy_required: Task energy level (High/Medium/Low).

    Returns:
        Celebration message string.

    Note: task_title is intentionally not logged to protect user privacy.
    """
    # Stub implementation — PR-B5 implements full reward subsystem
    celebration = _basic_celebration(streak=streak, work_type=work_type)
    log.info(
        "maybe_reward.delivered",
        peer=peer,
        notion_page_id=notion_page_id,
        streak=streak,
        work_type=work_type,
        # task_title intentionally omitted — private data
    )
    return celebration


def _basic_celebration(*, streak: int, work_type: str) -> str:
    """Return a basic celebration string based on streak and work type.

    Full intensity-mapped rewards in PR-B5. This stub covers:
    - Single completion
    - 3-task streak
    - 5+ task streak
    - Focus-specific completion
    """
    if streak >= 5:
        return "UNSTOPPABLE! 🔥🎉✨💪🚀"
    if streak >= 3:
        return "Hat trick! 🎩✨🎉"
    if work_type.lower() == "focus":
        return "Deep work done! 🧠✨"
    return "Nice work! ✨"


async def generate_reward_image(
    *,
    intensity: str,
    streak_count: int,
    task_descriptions: list[str],
    work_type: str = "",
    energy_level: str = "",
    sensitive_task: bool = False,
) -> str | None:
    """Generate an AI reward image via OpenAI gpt-image-1.

    Full implementation in PR-B5. This stub returns None (no image).

    Args:
        intensity: "low", "medium", "high", or "epic"
        streak_count: Current streak count (must equal len(task_descriptions))
        task_descriptions: List of completed task descriptions (private — never logged)
        work_type: Optional work type context hint
        energy_level: Optional energy level context hint
        sensitive_task: If True, uses metaphorical imagery only

    Returns:
        Absolute path to generated PNG, or None if generation unavailable.

    Note: task_descriptions are private — never logged, never committed.
    """
    # PR-B5 implements full image generation
    return None
