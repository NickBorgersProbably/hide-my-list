"""Reward subsystem for hide-my-list.

v1 scope per docs/reward-system.md:
- Emoji rewards (intensity-mapped)
- AI-generated celebration images via OpenAI gpt-image-1
- Sensitive-task guardrails (docs/reward-system.md:302-348)
- Feedback weighting (docs/reward-system.md:361-445)
- Fallback rewards when image gen fails
- Weekly recap (image compilation)

Deferred to v1.1:
- Audio rewards (home audio integration)
- Outing suggestions

See docs/python-rewrite/reward-deferred.md for deferred feature details.

Private data discipline (Codex F018):
- task_title is NEVER written to any log output
- reward_manifests Postgres table stores task_title (private column)
- Generated images stored under reward_artifacts Docker volume mount
- Test fixtures must NOT contain real task_title values
"""
from __future__ import annotations

import hashlib
import os
import random
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_ENABLE_LANGGRAPH_PATH = os.environ.get("ENABLE_LANGGRAPH_PATH", "false").lower() in (
    "true", "1", "yes"
)

# ---------------------------------------------------------------------------
# Sensitive task classification
# docs/reward-system.md:302-348
# ---------------------------------------------------------------------------

_SENSITIVE_KEYWORDS: frozenset[str] = frozenset([
    # Therapy / mental health
    "therapy", "therapist", "counseling", "counselor", "psychiatrist", "psychiatry",
    "mental health", "psychology", "psychologist", "anxiety", "depression",
    # Medical
    "doctor", "physician", "medical", "hospital", "clinic", "diagnosis",
    "medication", "prescription", "surgery", "appointment",
    # Legal
    "lawyer", "attorney", "legal", "court", "lawsuit", "contract",
    # Financial
    "taxes", "tax return", "irs", "bankruptcy", "debt", "financial advisor",
    # Personal admin
    "divorce", "custody", "funeral", "estate",
])


def is_sensitive_task(task_title: str) -> bool:
    """Classify whether a task title is sensitive (private/shame-heavy).

    Sensitive tasks receive suppressed or muted rewards:
    - task_mode forced to metaphorical
    - no literal task artifacts in imagery
    - humor forced to subtle

    Args:
        task_title: Task title string (private — not logged by this function).

    Returns:
        True if the task is classified as sensitive.
    """
    title_lower = task_title.lower()
    return any(keyword in title_lower for keyword in _SENSITIVE_KEYWORDS)


# ---------------------------------------------------------------------------
# Intensity scoring
# docs/reward-system.md: Score Calculation section
# ---------------------------------------------------------------------------

def compute_intensity(
    *,
    time_estimate: int,
    energy_required: str,
    streak: int,
    is_parent_complete: bool = False,
    is_all_cleared: bool = False,
    rewards_in_last_hour: int = 0,
    trigger: str = "completion",
    initiation_base_weight: float = 1.0,
    initiation_ceiling: int = 100,
) -> tuple[str, int]:
    """Compute reward intensity level and score.

    Implements the unified scoring algorithm from docs/reward-system.md.
    Returns (intensity_label, score).

    Args:
        time_estimate: Task time estimate in minutes.
        energy_required: "High", "Medium", or "Low".
        streak: Current consecutive completion streak.
        is_parent_complete: True if all sub-tasks of a parent task are done.
        is_all_cleared: True if all pending tasks cleared.
        rewards_in_last_hour: Count of rewards delivered in the past hour (for diminishing returns).
        trigger: "completion" or initiation trigger name.
        initiation_base_weight: Per-trigger multiplier (1.0 for completion).
        initiation_ceiling: Per-trigger score cap (100 for completion).

    Returns:
        Tuple of (intensity_label, score) where intensity_label is one of:
        "lightest", "low", "medium", "high", "epic".
    """
    energy_map = {"High": 3, "Medium": 2, "Low": 1}
    energy_value = energy_map.get(energy_required, 2)

    # Base score
    base_score = (time_estimate / 15) * 10 + (energy_value * 10)

    # Streak bonus
    streak_bonus = streak * 5

    # Milestone bonuses
    milestone_bonus = 0
    if is_parent_complete:
        milestone_bonus += 25
    if is_all_cleared:
        milestone_bonus += 50

    raw_score = base_score + streak_bonus + milestone_bonus

    # Diminishing returns
    diminishing = max(0, (rewards_in_last_hour - 2) * 10)

    if trigger == "completion":
        score = int(min(100, max(0, raw_score - diminishing)))
    else:
        # Initiation triggers
        weighted_score = (base_score * initiation_base_weight) + streak_bonus
        score = int(min(initiation_ceiling, max(0, weighted_score - diminishing)))

    # Map to intensity levels
    if score <= 10:
        return "lightest", score
    if score <= 25:
        return "low", score
    if score <= 50:
        return "medium", score
    if score <= 75:
        return "high", score
    return "epic", score


# ---------------------------------------------------------------------------
# Emoji celebration
# docs/reward-system.md: Emoji Celebrations section
# ---------------------------------------------------------------------------

_EMOJI_TEMPLATES: dict[str, list[str]] = {
    "lightest": ["Nice."],
    "low": ["Nice work! ✨", "Done! 💫", "Got it! ✅", "Speed demon! ⚡"],
    "medium": ["Deep work done! 🧠✨", "Hat trick! 🎩✨🎉", "Crushing it! 🎉✨💪", "Three down! 🔥💪"],
    "high": ["UNSTOPPABLE! 🔥🎉✨💪🚀", "On fire! 🔥🔥🔥✨💪", "Beast mode! 💪🔥🎉", "Conquered! ⚔️✨🏆"],
    "epic": [
        "LEGENDARY! 🏆👑🔥🎉✨💪🚀⭐",
        "MAJOR WIN! 🏆👑🎉✨🔥",
        "INBOX ZERO! 🏆👑✨🎉🔥💪🚀",
        "LEGENDARY DAY! 👑⭐🏆🎊",
        "PROJECT COMPLETE! 🚀⭐💪🎊",
    ],
}


def get_celebration_emoji(intensity: str, sensitive_task: bool = False) -> str:
    """Return an emoji celebration string for the given intensity.

    Args:
        intensity: One of "lightest", "low", "medium", "high", "epic".
        sensitive_task: If True, returns a muted response (no emoji).

    Returns:
        Celebration string.
    """
    if sensitive_task:
        # Muted reward for sensitive tasks — calm and warm, no fanfare.
        # "That took courage." can over-label routine private tasks as emotionally loaded.
        # "That mattered." is neutral, affirming, and applies to any private task category.
        return "Done. That mattered."

    templates = _EMOJI_TEMPLATES.get(intensity, _EMOJI_TEMPLATES["low"])
    return random.choice(templates)


# ---------------------------------------------------------------------------
# Fallback reward pool
# docs/reward-system.md: Graceful Degradation section
# ---------------------------------------------------------------------------

_FALLBACK_REWARDS: list[str] = [
    "Treat yourself to your favorite snack.",
    "30 minutes of your favorite game — you've earned it.",
    "Fancy coffee or hot chocolate time.",
    "Take a walk outside — fresh air after good work.",
    "Stretch or do a few yoga poses.",
    "Mini dance party in your living room.",
    "Call a friend and celebrate.",
    "Watch an episode of something you love.",
    "Order your favorite takeout.",
    "A cupcake or small treat.",
    "Ice cream — classic reward.",
    "Square of good chocolate.",
]


def get_fallback_reward() -> str:
    """Return a fun non-digital real-life reward suggestion.

    Used when image generation is unavailable.
    """
    return random.choice(_FALLBACK_REWARDS)


# ---------------------------------------------------------------------------
# Image generation
# docs/reward-system.md: AI-Generated Celebration Images section
# ---------------------------------------------------------------------------

_THEME_POOLS: dict[str, list[dict[str, str]]] = {
    "low": [
        {"theme": "cheerful bird with sparkle", "style": "watercolor", "palette": "warm pastel"},
        {"theme": "paper airplane soaring through clouds", "style": "paper collage", "palette": "soft blue"},
        {"theme": "happy cat in sunbeam", "style": "storybook illustration", "palette": "cozy warm"},
        {"theme": "small garden with blooming flowers", "style": "watercolor", "palette": "nature green"},
        {"theme": "cozy reading nook with warm light", "style": "impressionist", "palette": "amber glow"},
    ],
    "medium": [
        {"theme": "fox dancing in wildflowers", "style": "vibrant illustration", "palette": "jewel tones"},
        {"theme": "confetti explosion in bright colors", "style": "graphic art", "palette": "rainbow"},
        {"theme": "otter sliding down rainbow waterfall", "style": "cartoon", "palette": "neon pastel"},
        {"theme": "butterfly emerging from cocoon in golden light", "style": "watercolor", "palette": "golden"},
        {"theme": "mountain summit with celebration flags", "style": "adventure illustration", "palette": "crisp blue"},
    ],
    "high": [
        {"theme": "phoenix rising from golden flames", "style": "majestic illustration", "palette": "fire gold"},
        {"theme": "astronaut planting flag on colorful planet", "style": "space art", "palette": "cosmic purple"},
        {"theme": "whale breaching in starfield", "style": "surreal art", "palette": "midnight blue"},
        {"theme": "ancient temple lit by aurora borealis", "style": "epic landscape", "palette": "aurora jewel"},
        {"theme": "eagle soaring above mountain range at dawn", "style": "realistic", "palette": "sunrise red"},
    ],
    "epic": [
        {"theme": "galaxy forming crown of light", "style": "cosmic art", "palette": "nebula purple"},
        {"theme": "reality folding into cathedral of light", "style": "transcendent digital art", "palette": "iridescent"},
        {"theme": "cosmic phoenix ascending through dimensional portal", "style": "epic sci-fi", "palette": "gold and violet"},
        {"theme": "universe crystallizing into perfect order", "style": "abstract cosmic", "palette": "prismatic"},
        {"theme": "ancient forest where trees become stars", "style": "mythic illustration", "palette": "silver and emerald"},
    ],
}

_SENSITIVE_THEMES: list[dict[str, str]] = [
    {"theme": "abstract geometric pattern expanding outward", "style": "minimalist", "palette": "calm blue-grey"},
    {"theme": "smooth river stones arranged in peaceful pattern", "style": "zen illustration", "palette": "earth tones"},
    {"theme": "gentle light through frosted glass", "style": "abstract", "palette": "soft white"},
    {"theme": "growing seedling in quiet soil", "style": "simple illustration", "palette": "natural green"},
    {"theme": "single candle flame in dark, steady and bright", "style": "minimal", "palette": "warm amber"},
]


def _build_image_prompt(
    *,
    intensity: str,
    streak_count: int,
    task_descriptions: list[str],
    user_prefs: dict[str, Any] | None = None,
    sensitive_task: bool = False,
    feedback_history: list[dict] | None = None,
) -> str:
    """Build a personalized OpenAI image generation prompt.

    Task descriptions are used to classify task motifs but are NOT copied
    verbatim into the prompt (private data discipline).

    Args:
        intensity: "low", "medium", "high", or "epic"
        streak_count: Current streak count
        task_descriptions: List of completed task descriptions (private — not embedded in prompt)
        user_prefs: Optional reward preferences dict
        sensitive_task: If True, uses abstract/symbolic themes only
        feedback_history: Optional recent feedback for theme weighting

    Returns:
        Image generation prompt string (does not contain task_title verbatim).
    """
    prefs = user_prefs or {}

    # Select theme from pool
    if sensitive_task:
        theme_pool = _SENSITIVE_THEMES
    else:
        theme_pool = _THEME_POOLS.get(intensity, _THEME_POOLS["low"])

    # Apply feedback weighting (simple: avoid recently negatively-rated theme families)
    if feedback_history:
        neg_themes = {f.get("theme_family", "") for f in feedback_history if f.get("score", 0) < 0}
        weighted_pool = [t for t in theme_pool if t.get("theme", "") not in neg_themes]
        if weighted_pool:
            theme_pool = weighted_pool

    theme = random.choice(theme_pool)

    # Apply user style preferences
    style = theme["style"]
    palette = theme["palette"]
    if prefs.get("preferred_styles"):
        style = random.choice(prefs["preferred_styles"])
    if prefs.get("preferred_palettes"):
        palette = random.choice(prefs["preferred_palettes"])

    # Avoid list
    avoid_str = ""
    if prefs.get("avoid"):
        avoid_str = f" Avoid: {', '.join(prefs['avoid'])}."

    # Humor level
    humor = prefs.get("humor_level", "subtle")
    if sensitive_task:
        humor = "subtle"

    # Build streak marker description
    if streak_count == 1:
        streak_str = "one small glowing progress marker"
    else:
        streak_str = f"exactly {streak_count} small glowing progress markers"

    prompt = (
        f"A {style} artwork in {palette} color palette. "
        f"Theme: {theme['theme']}. "
        f"Mood: celebratory, uplifting, {humor} energy. "
        f"Include {streak_str} subtly integrated into the composition. "
        f"Professional quality, no text, no words, no letters.{avoid_str} "
        f"High resolution, clean composition."
    )

    return prompt


async def generate_reward_image(
    *,
    intensity: str,
    streak_count: int,
    task_descriptions: list[str],
    work_type: str = "",
    energy_level: str = "",
    sensitive_task: bool = False,
    user_prefs: dict[str, Any] | None = None,
    feedback_history: list[dict] | None = None,
) -> str | None:
    """Generate an AI celebration image via OpenAI gpt-image-1.

    Implements docs/reward-system.md: AI-Generated Celebration Images.

    Private data discipline:
    - task_descriptions are used for motif classification only, never embedded verbatim
    - Generated images stored under reward_artifacts volume (env REWARD_ARTIFACTS_DIR)
    - Returns absolute path to PNG, or None on failure

    Args:
        intensity: "low", "medium", "high", or "epic"
        streak_count: Current streak count (must equal len(task_descriptions))
        task_descriptions: Completed task descriptions (private — classified, not copied)
        work_type: Optional work type hint
        energy_level: Optional energy level hint
        sensitive_task: If True, uses abstract imagery only
        user_prefs: Optional user reward preferences
        feedback_history: Optional feedback list for theme weighting

    Returns:
        Absolute path to generated PNG file, or None on failure.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        log.debug("generate_reward_image.no_api_key")
        return None

    if intensity == "lightest":
        # Lightest tier doesn't get image rewards
        return None

    # Validate inputs
    if streak_count < 1:
        log.warning("generate_reward_image.invalid_streak_count", streak_count=streak_count)
        streak_count = 1
    if len(task_descriptions) != streak_count:
        log.warning(
            "generate_reward_image.description_count_mismatch",
            streak_count=streak_count,
            description_count=len(task_descriptions),
        )
        # Truncate or pad to match streak
        task_descriptions = task_descriptions[:streak_count]

    # Reject empty descriptions
    task_descriptions = [d for d in task_descriptions if d.strip()]
    if not task_descriptions:
        log.warning("generate_reward_image.empty_descriptions")
        return None

    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])

        prompt = _build_image_prompt(
            intensity=intensity,
            streak_count=streak_count,
            task_descriptions=task_descriptions,
            user_prefs=user_prefs,
            sensitive_task=sensitive_task,
            feedback_history=feedback_history,
        )

        quality = "high" if intensity == "epic" else "auto"

        response = await client.images.generate(
            model="gpt-image-1",
            prompt=prompt,
            size="1024x1024",
            quality=quality,
            response_format="b64_json",
            n=1,
        )

        image_data = response.data[0].b64_json
        if not image_data:
            log.warning("generate_reward_image.empty_response")
            return None

        # Save to artifact path
        artifacts_dir = Path(
            os.environ.get("REWARD_ARTIFACTS_DIR", "/tmp/reward_artifacts")
        )
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(UTC).strftime("%Y-%m-%d_%H%M%S")
        filename = f"{timestamp}_{intensity}.png"
        output_path = artifacts_dir / filename

        import base64
        output_path.write_bytes(base64.b64decode(image_data))

        log.info(
            "generate_reward_image.success",
            intensity=intensity,
            # task_descriptions intentionally not logged — private data
        )
        return str(output_path)

    except Exception:
        log.exception("generate_reward_image.failed", intensity=intensity)
        return None


# ---------------------------------------------------------------------------
# Weekly recap
# docs/reward-system.md: Weekly Recap section
# ---------------------------------------------------------------------------

async def generate_weekly_recap(
    *,
    peer: str,
    days_back: int = 7,
    artifacts_dir: str | None = None,
) -> str | None:
    """Compile reward images from the past week into a summary.

    v1 implementation: finds PNG files from the past N days in the artifacts dir
    and returns a text summary (video compilation deferred to v1.1).

    Args:
        peer: E.164 peer identifier.
        days_back: Number of days to look back (default 7).
        artifacts_dir: Override path to reward artifacts directory.

    Returns:
        Path to generated recap file, or None if no images available.
    """
    dir_path = Path(artifacts_dir or os.environ.get("REWARD_ARTIFACTS_DIR", "/tmp/reward_artifacts"))

    if not dir_path.is_dir():
        log.info("generate_weekly_recap.no_artifacts_dir", peer=peer)
        return None

    cutoff = datetime.now(UTC).timestamp() - (days_back * 86400)
    images = [
        p for p in dir_path.glob("*.png")
        if p.stat().st_mtime >= cutoff
    ]

    if not images:
        log.info("generate_weekly_recap.no_images", peer=peer, days_back=days_back)
        return None

    # v1: generate a text recap (video compilation in v1.1)
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d")
    recap_path = dir_path / f"weekly-recap-{timestamp}.txt"
    recap_path.write_text(
        f"Weekly recap: {len(images)} completions in the past {days_back} days. Keep it up!\n",
        encoding="utf-8",
    )

    log.info("generate_weekly_recap.done", peer=peer, image_count=len(images))
    return str(recap_path)


# ---------------------------------------------------------------------------
# Feedback weighting
# docs/reward-system.md:361-445
# ---------------------------------------------------------------------------

def apply_feedback_weight(
    feedback_history: list[dict[str, Any]],
    theme_family: str,
    style: str,
    palette: str,
) -> float:
    """Compute a selection weight for a theme/style/palette based on feedback history.

    Implements bounded feedback weighting: recent reactions decay over time,
    aggregate weights are capped, result is a nudge not a dictate.

    Args:
        feedback_history: List of dicts with keys: score (int), theme_family (str),
            style (str), palette (str), timestamp (str ISO 8601).
        theme_family: Theme family to compute weight for.
        style: Style to compute weight for.
        palette: Palette to compute weight for.

    Returns:
        Weight float between 0.5 and 2.0. 1.0 is neutral.
        >1.0 means positive bias, <1.0 means negative bias.
    """
    if not feedback_history:
        return 1.0

    now = datetime.now(UTC)
    total_nudge = 0.0

    for entry in feedback_history:
        score = entry.get("score", 0)
        entry_theme = entry.get("theme_family", "")
        entry_style = entry.get("style", "")
        entry_palette = entry.get("palette", "")
        entry_ts_str = entry.get("timestamp", "")

        # Check relevance
        match_weight = 0.0
        if entry_theme == theme_family:
            match_weight += 0.6
        if entry_style == style:
            match_weight += 0.3
        if entry_palette == palette:
            match_weight += 0.1

        if match_weight == 0:
            continue

        # Time decay: entries older than 30 days have 0 effect
        try:
            entry_ts = datetime.fromisoformat(entry_ts_str.replace("Z", "+00:00"))
            age_days = (now - entry_ts).days
        except (ValueError, TypeError):
            age_days = 30

        if age_days >= 30:
            continue

        decay = 1.0 - (age_days / 30.0)

        # Contribution: score ∈ {-1, 0, 1} × match × decay
        contribution = score * match_weight * decay
        total_nudge += contribution

    # Cap total nudge at ±0.5 (so weight stays in [0.5, 1.5])
    total_nudge = max(-0.5, min(0.5, total_nudge))
    return 1.0 + total_nudge


# ---------------------------------------------------------------------------
# Manifest writing
# docs/reward-system.md: Feedback Loop section
# ---------------------------------------------------------------------------

async def write_reward_manifest(
    *,
    peer: str,
    notion_page_id: str,
    task_title: str,
    reward_kind: str,
    intensity: str,
    streak_count: int,
    delivered_at: datetime,
    artifact_path: str | None = None,
    sensitive_task: bool = False,
) -> uuid.UUID | None:
    """Write a reward delivery record to the reward_manifests Postgres table.

    Private data discipline:
    - task_title is stored in Postgres (private column) but NEVER written to logs
    - artifact_path is a local filesystem path, never committed to repo

    Returns the UUID of the inserted manifest row, or None on failure.
    """
    from app.tools.db import get_db_conn

    manifest_id = uuid.uuid4()
    try:
        async with get_db_conn() as conn:
            await conn.execute(
                """
                INSERT INTO reward_manifests
                  (id, peer, notion_page_id, task_title, reward_kind, intensity,
                   streak_count, delivered_at, artifact_path, sensitive_task)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(manifest_id),
                    peer,
                    notion_page_id,
                    task_title,  # Private — stored in DB, never logged
                    reward_kind,
                    intensity,
                    streak_count,
                    delivered_at,
                    artifact_path,
                    sensitive_task,
                ),
            )
        # Log without task_title — private data discipline
        log.info(
            "write_reward_manifest.ok",
            manifest_id=str(manifest_id),
            peer=peer,
            notion_page_id=notion_page_id,
            intensity=intensity,
            # task_title intentionally omitted
        )
        return manifest_id
    except Exception:
        log.exception(
            "write_reward_manifest.failed",
            peer=peer,
            notion_page_id=notion_page_id,
        )
        return None


# ---------------------------------------------------------------------------
# Main entry point: maybe_reward()
# Called by COMPLETE node
# ---------------------------------------------------------------------------

async def maybe_reward(
    *,
    peer: str,
    task_title: str,
    notion_page_id: str,
    streak: int,
    work_type: str = "",
    energy_required: str = "",
    time_estimate: int = 30,
    is_parent_complete: bool = False,
    is_all_cleared: bool = False,
    rewards_in_last_hour: int = 0,
    user_prefs: dict[str, Any] | None = None,
) -> str:
    """Generate and deliver a complete reward for a task completion.

    Implements docs/reward-system.md: Completion Flow Enhancement.
    Private data discipline: task_title is passed through but never logged.

    Args:
        peer: E.164 recipient phone number.
        task_title: Completed task title (PRIVATE — never log).
        notion_page_id: Notion page ID of the completed task.
        streak: Post-completion streak count.
        work_type: Task work type.
        energy_required: Task energy level.
        time_estimate: Task time estimate in minutes.
        is_parent_complete: True if all sub-tasks of a parent are done.
        is_all_cleared: True if all tasks cleared.
        rewards_in_last_hour: Recent reward count for diminishing returns.
        user_prefs: Optional user reward preferences.

    Returns:
        Celebration message string (emoji text ± image path as MEDIA: line).
    """
    # Classify sensitive task
    sensitive = is_sensitive_task(task_title)

    # Compute intensity
    intensity_label, score = compute_intensity(
        time_estimate=time_estimate,
        energy_required=energy_required,
        streak=streak,
        is_parent_complete=is_parent_complete,
        is_all_cleared=is_all_cleared,
        rewards_in_last_hour=rewards_in_last_hour,
        trigger="completion",
    )

    # Get emoji celebration
    celebration_text = get_celebration_emoji(intensity_label, sensitive_task=sensitive)

    # Attempt image generation
    image_path = None
    if intensity_label != "lightest" and not sensitive:
        prefs = (user_prefs or {}).get("rewards") if user_prefs else None
        image_path = await generate_reward_image(
            intensity=intensity_label,
            streak_count=streak,
            task_descriptions=[task_title],  # Private — classified, not embedded in prompt
            work_type=work_type,
            energy_level=energy_required.lower(),
            sensitive_task=sensitive,
            user_prefs=prefs,
        )
    elif sensitive:
        # Sensitive: muted emoji only (no image)
        image_path = None

    # Fallback if image gen failed
    reward_kind = "emoji"
    if image_path:
        reward_kind = "emoji+image"
        celebration_text = f"{celebration_text}\nMEDIA:{image_path}"
    elif intensity_label in ("medium", "high", "epic") and not sensitive:
        # Image was expected but failed — add fallback
        fallback = get_fallback_reward()
        celebration_text = f"{celebration_text}\n{fallback}"
        reward_kind = "image_fallback"

    # Write manifest (non-blocking — failure doesn't break reward delivery)
    delivered_at = datetime.now(UTC)
    try:
        await write_reward_manifest(
            peer=peer,
            notion_page_id=notion_page_id,
            task_title=task_title,  # Private — stored in Postgres only
            reward_kind=reward_kind,
            intensity=intensity_label,
            streak_count=streak,
            delivered_at=delivered_at,
            artifact_path=image_path,
            sensitive_task=sensitive,
        )
    except Exception:
        log.exception(
            "maybe_reward.manifest_failed",
            peer=peer,
            notion_page_id=notion_page_id,
            # task_title intentionally omitted
        )

    log.info(
        "maybe_reward.delivered",
        peer=peer,
        notion_page_id=notion_page_id,
        intensity=intensity_label,
        streak=streak,
        reward_kind=reward_kind,
        sensitive=sensitive,
        # task_title intentionally omitted — private data
    )

    return celebration_text
