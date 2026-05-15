"""LangChain provider adapter factory for hide-my-list.

Reads model tier assignments from setup/model-tiers.json and exposes
a single llm(tier) factory function. Validates model IDs at startup.

Tiers:
  expensive -> claude-opus-4-6 (complex reasoning: GET_TASK scoring)
  medium    -> claude-sonnet-4-6 (intent classification, most nodes)
  cheap     -> claude-haiku-4-5 (lightweight tasks)
  reminder  -> claude-haiku-4-5 (reminder delivery cron)

LangSmith guard: refuses boot when LANGSMITH_TRACING=true unless
ALLOW_PRIVATE_TRACE_EXPORT=true is also set. Private user data (task titles,
user messages) must never be exported to LangSmith by default.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from langchain_anthropic import ChatAnthropic

_REPO_ROOT = Path(__file__).parent.parent
_MODEL_TIERS_PATH = _REPO_ROOT / "setup" / "model-tiers.json"

Tier = Literal["expensive", "medium", "cheap", "reminder"]

_VALID_TIERS: frozenset[str] = frozenset(["expensive", "medium", "cheap", "reminder"])

# Anthropic model prefix for validation
_ANTHROPIC_PREFIX = "claude-"


def _check_langsmith_guard() -> None:
    """Refuse startup when LangSmith tracing is enabled without explicit opt-in.

    Private user data (task titles, conversation messages) must never be exported
    to LangSmith by default. Operator must set ALLOW_PRIVATE_TRACE_EXPORT=true
    to acknowledge this risk.
    """
    tracing_on = os.environ.get("LANGSMITH_TRACING", "").lower() in ("true", "1", "yes")
    allowed = os.environ.get("ALLOW_PRIVATE_TRACE_EXPORT", "").lower() in ("true", "1", "yes")
    if tracing_on and not allowed:
        raise RuntimeError(
            "LANGSMITH_TRACING=true is set but ALLOW_PRIVATE_TRACE_EXPORT is not. "
            "LangSmith would export private user data (task titles, messages) to an "
            "external service. Set ALLOW_PRIVATE_TRACE_EXPORT=true to acknowledge "
            "this risk and enable tracing, or unset LANGSMITH_TRACING."
        )


@lru_cache(maxsize=1)
def _load_model_tiers() -> dict[str, str]:
    """Load and validate model-tiers.json. Cached — called once at startup."""
    _check_langsmith_guard()

    if not _MODEL_TIERS_PATH.is_file():
        raise RuntimeError(f"model-tiers.json not found at {_MODEL_TIERS_PATH}")

    data = json.loads(_MODEL_TIERS_PATH.read_text(encoding="utf-8"))

    missing = _VALID_TIERS - set(data.keys())
    if missing:
        raise RuntimeError(
            f"setup/model-tiers.json is missing required tiers: {sorted(missing)}. "
            f"Expected tiers: {sorted(_VALID_TIERS)}"
        )

    # Validate model IDs — must be Anthropic models for langchain-anthropic
    for tier, model_id in data.items():
        if tier not in _VALID_TIERS:
            continue  # Extra keys are ignored
        if not isinstance(model_id, str) or not model_id.startswith(_ANTHROPIC_PREFIX):
            raise RuntimeError(
                f"setup/model-tiers.json tier '{tier}' has invalid model ID '{model_id}'. "
                f"Anthropic models must start with '{_ANTHROPIC_PREFIX}'."
            )

    return data


def llm(tier: Tier, *, temperature: float = 0.0) -> ChatAnthropic:
    """Return a LangChain ChatAnthropic instance for the given tier.

    Model IDs are resolved from setup/model-tiers.json, validated at first call.
    The ANTHROPIC_API_KEY environment variable must be set.

    Args:
        tier: One of 'expensive', 'medium', 'cheap', 'reminder'.
        temperature: Sampling temperature. Defaults to 0.0 for deterministic output.

    Returns:
        ChatAnthropic configured for the specified tier.

    Raises:
        RuntimeError: If model-tiers.json is missing or malformed, or if
            LANGSMITH_TRACING=true without ALLOW_PRIVATE_TRACE_EXPORT.
        ValueError: If tier is not a valid tier name.
    """
    if tier not in _VALID_TIERS:
        raise ValueError(
            f"Unknown model tier '{tier}'. Valid tiers: {sorted(_VALID_TIERS)}"
        )

    tiers = _load_model_tiers()
    model_id = tiers[tier]

    return ChatAnthropic(
        model=model_id,
        temperature=temperature,
        max_tokens=1024,
    )


def validate_startup() -> None:
    """Call at application startup to eagerly validate model configuration.

    Raises RuntimeError if setup/model-tiers.json is missing, incomplete, or
    contains invalid model IDs, or if LangSmith guard fires.
    """
    _load_model_tiers()
