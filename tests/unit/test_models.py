"""Tests for app/models.py — LLM provider factory and LangSmith guard."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest


def test_model_tiers_json_exists() -> None:
    """setup/model-tiers.json must exist and contain all required tiers."""
    tiers_path = Path(__file__).parent.parent.parent / "setup" / "model-tiers.json"
    assert tiers_path.is_file(), f"model-tiers.json not found at {tiers_path}"

    import json
    data = json.loads(tiers_path.read_text(encoding="utf-8"))

    required_tiers = {"expensive", "medium", "cheap", "reminder"}
    missing = required_tiers - set(data.keys())
    assert not missing, f"model-tiers.json missing tiers: {sorted(missing)}"


def test_all_tiers_have_anthropic_model_ids() -> None:
    """All model IDs in model-tiers.json must start with 'claude-'."""
    import json
    tiers_path = Path(__file__).parent.parent.parent / "setup" / "model-tiers.json"
    data = json.loads(tiers_path.read_text(encoding="utf-8"))

    for tier, model_id in data.items():
        if tier in {"expensive", "medium", "cheap", "reminder"}:
            assert model_id.startswith("claude-"), (
                f"Tier '{tier}' model '{model_id}' must start with 'claude-'"
            )


def test_langsmith_guard_fires_when_tracing_without_export_flag() -> None:
    """LangSmith guard must refuse startup when LANGSMITH_TRACING=true without export flag."""
    from app.models import _load_model_tiers  # noqa: F401

    # Reset the lru_cache so the guard runs fresh
    from app import models as models_module
    models_module._load_model_tiers.cache_clear()

    with patch.dict(
        os.environ,
        {"LANGSMITH_TRACING": "true"},
        clear=False,
    ):
        # Ensure ALLOW_PRIVATE_TRACE_EXPORT is not set
        env = dict(os.environ)
        env.pop("ALLOW_PRIVATE_TRACE_EXPORT", None)

        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="ALLOW_PRIVATE_TRACE_EXPORT"):
                models_module._load_model_tiers.cache_clear()
                models_module._load_model_tiers()

    # Reset cache after test
    models_module._load_model_tiers.cache_clear()


def test_langsmith_guard_passes_when_export_explicitly_allowed() -> None:
    """LangSmith guard must pass when ALLOW_PRIVATE_TRACE_EXPORT=true."""
    from app import models as models_module
    models_module._load_model_tiers.cache_clear()

    with patch.dict(
        os.environ,
        {
            "LANGSMITH_TRACING": "true",
            "ALLOW_PRIVATE_TRACE_EXPORT": "true",
        },
        clear=False,
    ):
        # Should not raise
        result = models_module._load_model_tiers()
        assert "expensive" in result

    models_module._load_model_tiers.cache_clear()


def test_langsmith_guard_passes_when_tracing_disabled() -> None:
    """LangSmith guard must not fire when LANGSMITH_TRACING is not set."""
    from app import models as models_module
    models_module._load_model_tiers.cache_clear()

    env = dict(os.environ)
    env.pop("LANGSMITH_TRACING", None)
    env.pop("ALLOW_PRIVATE_TRACE_EXPORT", None)

    with patch.dict(os.environ, env, clear=True):
        result = models_module._load_model_tiers()
        assert "expensive" in result
        assert "reminder" in result

    models_module._load_model_tiers.cache_clear()


def test_valid_tier_returns_llm_instance() -> None:
    """llm(tier) must return a ChatAnthropic instance for valid tiers."""
    from app import models as models_module
    models_module._load_model_tiers.cache_clear()

    env = dict(os.environ)
    env.pop("LANGSMITH_TRACING", None)
    env.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

    with patch.dict(os.environ, env, clear=True):
        from langchain_anthropic import ChatAnthropic
        result = models_module.llm("medium")
        assert isinstance(result, ChatAnthropic)

    models_module._load_model_tiers.cache_clear()


def test_invalid_tier_raises_value_error() -> None:
    """llm() with unknown tier must raise ValueError."""
    from app import models as models_module
    models_module._load_model_tiers.cache_clear()

    env = dict(os.environ)
    env.pop("LANGSMITH_TRACING", None)
    env.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValueError, match="Unknown model tier"):
            models_module.llm("nonexistent_tier")  # type: ignore[arg-type]

    models_module._load_model_tiers.cache_clear()
