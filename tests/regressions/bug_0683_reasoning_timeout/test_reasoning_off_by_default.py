"""Intake and the reply nodes send think=false; only the measured callers reason.

Regression for the proxy-timeout cascade described in README.md. The
assertion is on the constructed ChatOpenAI payload, not on a live call, so it
runs without a model host.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

_TIERS = ("cheap", "medium", "expensive", "reminder")
_OFF_CALLERS = ("intake", "selection", "chat", "rejection", "check_in", "complete_title_match", "classify", None)
_ON_CALLERS = ("cannot_finish", "need_help", "interaction_review")


def _env(reasoning_callers: str | None) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("LANGSMITH_TRACING", None)
    env.pop("LLM_REASONING_CALLERS", None)
    env.setdefault("LLM_PROXY_API_KEY", "test-key-not-used")
    env.setdefault("LLM_PROXY_BASE_URL", "https://proxy.test/v1")
    if reasoning_callers is not None:
        env["LLM_REASONING_CALLERS"] = reasoning_callers
    return env


@pytest.mark.parametrize("tier", _TIERS)
def test_intake_and_reply_callers_never_reason_by_default(tier: str) -> None:
    from app import models

    models._load_model_tiers.cache_clear()
    with patch.dict(os.environ, _env(None), clear=True):
        for caller in _OFF_CALLERS:
            assert models.llm(tier, caller=caller).bound.extra_body == {"think": False}, caller
        for caller in _ON_CALLERS:
            assert models.llm(tier, caller=caller).bound.extra_body == {"think": True}, caller
    models._load_model_tiers.cache_clear()


def test_every_call_carries_two_retries_by_default() -> None:
    """Two retries: the proxy's instant 500 after an idle gap repeats on the
    first ~0.5 s retry and clears on the next attempt."""
    from app import models

    models._load_model_tiers.cache_clear()
    env = _env(None)
    env.pop("LLM_MAX_RETRIES", None)
    with patch.dict(os.environ, env, clear=True):
        for tier in _TIERS:
            assert models.llm(tier, caller="intake").bound.max_retries == 2
    models._load_model_tiers.cache_clear()


def test_env_replaces_the_reasoning_set_and_empty_means_none() -> None:
    from app import models

    models._load_model_tiers.cache_clear()
    with patch.dict(os.environ, _env("intake"), clear=True):
        assert models.llm("medium", caller="intake").bound.extra_body == {"think": True}
        assert models.llm("medium", caller="need_help").bound.extra_body == {"think": False}
    with patch.dict(os.environ, _env(""), clear=True):
        for caller in _ON_CALLERS:
            assert models.llm("medium", caller=caller).bound.extra_body == {"think": False}
    models._load_model_tiers.cache_clear()
