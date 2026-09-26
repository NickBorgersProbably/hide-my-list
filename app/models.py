"""LangChain provider adapter factory for hide-my-list.

Reads model tier assignments from setup/model-tiers.json and exposes
a single llm(tier) factory function. Validates model IDs at startup.

Tiers (model alias resolves via setup/model-tiers.json; per-tier output caps
are set here):
  expensive -> gemma4-small, uncapped (GET_TASK scoring)
  medium    -> gemma4-small, uncapped (user-facing replies + intake's
                                        structured JSON)
  cheap     -> gemma4-small, max_tokens=1024 (label-only classification)
  reminder  -> gemma4-small, uncapped (reminder cron; currently no caller)

Reasoning ("think") is decided per caller, not per tier. The model host serves
one request at a time and the proxy in front of it gives up on any single
request after 110 s; with think on, intake spent 2k–4k tokens reasoning per
turn (47–103 s observed) and a single long chain of thought timed out the turn
and queued every conversation behind it. Think-off intake lands in ~12 s and
think-off selection in ~5 s with no eval regression. cannot_finish and
need_help lose accuracy without reasoning (measured on the eval suite), and
interaction_review misreads its verdict enum, so those three keep it by
default; the review runs in the background after the reply, so its latency
never reaches the user.
LLM_REASONING_CALLERS (comma-separated caller names) replaces that default for
a deployment; an empty value turns reasoning off everywhere.

Uncapped tiers send no max_tokens: structured JSON output must not be
truncated, or the partial JSON fails to parse and a reminder is silently
dropped. Only the label-only cheap tier carries an output cap (see
_TIER_MAX_TOKENS).

All tiers point at the same model alias because the LLM host can only
hold one Gemma model in RAM at a time. Tiers differ only in the output cap
today; reasoning is decided per caller, not per tier.

Model IDs are sent as OpenAI-format chat-completion requests to the
LiteLLM proxy at LLM_PROXY_BASE_URL. Adding a new provider family is
just adding its prefix to _VALID_MODEL_PREFIXES.

LangSmith guard: refuses boot when LANGSMITH_TRACING=true unless
ALLOW_PRIVATE_TRACE_EXPORT=true is also set. Private user data (task titles,
user messages) must never be exported to LangSmith by default.

Observability: llm() attaches an LLMObservabilityCallback to every returned
model instance. The callback emits llm.call.start / llm.call.end /
llm.call.error events via structlog, tagged with tier + caller. The caller
kwarg (short node name e.g. "intake", "chat") is optional but should always
be provided by graph nodes.

Latency ceiling: every call carries an explicit request timeout and retry cap
(see _request_timeout_seconds / _max_retries). Without them the OpenAI SDK
applies its own defaults, and a wedged model host turns into a multi-minute
hang per call — which, because the model backend serves one request at a time,
stalls every conversation queued behind it.
"""
from __future__ import annotations

import json
import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import structlog
from langchain_openai import ChatOpenAI

from app.observability.llm_callback import LLMObservabilityCallback

log = structlog.get_logger(__name__)

_REPO_ROOT = Path(__file__).parent.parent
_MODEL_TIERS_PATH = _REPO_ROOT / "setup" / "model-tiers.json"

Tier = Literal["expensive", "medium", "cheap", "reminder"]

_VALID_TIERS: frozenset[str] = frozenset(["expensive", "medium", "cheap", "reminder"])

# Known model-ID prefixes accepted by the LiteLLM proxy. A startup-time
# allowlist catches typos in setup/model-tiers.json before the first call.
_VALID_MODEL_PREFIXES: tuple[str, ...] = ("claude-", "gemma", "gpt-")

# Model-ID prefixes served by the self-hosted model on the tailnet rather than
# by an external provider. The proxy forwards `claude-` and `gpt-` aliases off
# the network; `gemma` is the locally-hosted family.
#
# This exists so a caller can refuse to send private data through a tier that
# has been swapped to an external provider. setup/model-tiers.json is editable
# without touching call sites, so a privacy promise made in prose is only worth
# as much as the check that enforces it — see is_local_tier().
_LOCAL_MODEL_PREFIXES: tuple[str, ...] = ("gemma",)

# Callers that run with reasoning ("think") on unless LLM_REASONING_CALLERS
# says otherwise. The proxy passes `think` straight through to the Ollama
# backend; every call sends the flag explicitly so the backend default never
# decides. Names are the `caller` values passed to llm().
_DEFAULT_REASONING_CALLERS: frozenset[str] = frozenset(
    {"cannot_finish", "need_help", "interaction_review"}
)
_REASONING_CALLERS_ENV = "LLM_REASONING_CALLERS"


def _reasoning_callers() -> frozenset[str]:
    """Callers that send think=true: LLM_REASONING_CALLERS when set, else the default."""
    raw = os.environ.get(_REASONING_CALLERS_ENV)
    if raw is None:
        return _DEFAULT_REASONING_CALLERS
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _caller_extra_body(caller: str | None) -> dict[str, Any]:
    """Extra request body for a call: the `think` flag, always explicit."""
    return {"think": caller is not None and caller in _reasoning_callers()}


# Per-tier output-token cap. Only the cheap tier is capped: its sole caller
# (intent classifier) emits a single label, so a small ceiling is free safety.
# The other tiers (expensive/medium/reminder) are intentionally absent — they
# emit structured JSON (e.g. intake's full task object), and a cap truncates
# that output mid-JSON. Truncated JSON then fails to parse and the task is
# silently saved without its reminder — so those tiers send no max_tokens and
# let the model finish.
_TIER_MAX_TOKENS: dict[str, int] = {
    "cheap": 1024,
}

# Per-request latency ceiling. The model backend holds one model in RAM and
# serves one request at a time, so an unbounded call does not just delay its own
# turn — it holds the only inference slot and every queued conversation waits
# behind it. Observed successful calls land between 0.5s and ~15s with
# reasoning off and up to ~40s for the reasoning callers; the default leaves
# room for queue wait behind another tenant on the same backend plus a slow
# reasoning turn, and still gives up long before the reverse proxy in front
# of the LiteLLM proxy synthesizes its own 504 at 600s. Failing on our own clock
# keeps the error ours to classify instead of an opaque gateway timeout.
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 120.0

# Attempts = _max_retries + 1. The proxy's first response after an idle gap is
# sometimes an instant 500 (LiteLLM reports "Timeout ... time taken=0.0"), and
# it arrives in pairs: the SDK's first retry lands ~0.5s later on the same
# stale connection and fails the same way, while a third attempt succeeds.
# Two retries absorb that; the worst case (3 x 120s = 360s) stays under the
# 600s gateway timeout.
_DEFAULT_MAX_RETRIES = 2


def _request_timeout_seconds() -> float:
    """Per-request timeout in seconds, overridable via LLM_REQUEST_TIMEOUT_SECONDS."""
    raw = os.environ.get(
        "LLM_REQUEST_TIMEOUT_SECONDS",
        str(_DEFAULT_REQUEST_TIMEOUT_SECONDS),
    )
    try:
        value = float(raw)
    except ValueError:
        log.warning(
            "models.invalid_request_timeout",
            configured_value=raw,
            fallback_seconds=_DEFAULT_REQUEST_TIMEOUT_SECONDS,
        )
        return _DEFAULT_REQUEST_TIMEOUT_SECONDS
    if value <= 0 or not math.isfinite(value):
        log.warning(
            "models.invalid_request_timeout",
            configured_value=raw,
            fallback_seconds=_DEFAULT_REQUEST_TIMEOUT_SECONDS,
        )
        return _DEFAULT_REQUEST_TIMEOUT_SECONDS
    return value


def _max_retries() -> int:
    """Retry count per call, overridable via LLM_MAX_RETRIES. 0 disables retries."""
    raw = os.environ.get("LLM_MAX_RETRIES", str(_DEFAULT_MAX_RETRIES))
    try:
        value = int(raw)
    except ValueError:
        log.warning(
            "models.invalid_max_retries",
            configured_value=raw,
            fallback_retries=_DEFAULT_MAX_RETRIES,
        )
        return _DEFAULT_MAX_RETRIES
    if value < 0:
        log.warning(
            "models.invalid_max_retries",
            configured_value=raw,
            fallback_retries=_DEFAULT_MAX_RETRIES,
        )
        return _DEFAULT_MAX_RETRIES
    return value


def _require_llm_proxy_config() -> tuple[str, str]:
    """Return required LiteLLM proxy config, failing fast when absent."""
    base_url = os.environ.get("LLM_PROXY_BASE_URL")
    api_key = os.environ.get("LLM_PROXY_API_KEY")
    if not base_url:
        raise RuntimeError(
            "LLM_PROXY_BASE_URL must point at the OpenAI-compatible LiteLLM /v1 endpoint"
        )
    if not api_key:
        raise RuntimeError("LLM_PROXY_API_KEY must be set for the LiteLLM proxy")
    return base_url, api_key


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

    raw_data = json.loads(_MODEL_TIERS_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw_data, dict):
        raise RuntimeError("setup/model-tiers.json must contain a JSON object")
    data: dict[str, str] = {}
    for key, value in raw_data.items():
        if isinstance(key, str) and isinstance(value, str):
            data[key] = value

    missing = _VALID_TIERS - set(data.keys())
    if missing:
        raise RuntimeError(
            f"setup/model-tiers.json is missing required tiers: {sorted(missing)}. "
            f"Expected tiers: {sorted(_VALID_TIERS)}"
        )

    # Validate model IDs against the known-prefix allowlist. The proxy is the
    # source of truth for which aliases actually resolve; this check only
    # catches obvious typos at startup.
    for tier, model_id in data.items():
        if tier not in _VALID_TIERS:
            continue  # Extra keys are ignored
        if not isinstance(model_id, str) or not any(
            model_id.startswith(p) for p in _VALID_MODEL_PREFIXES
        ):
            raise RuntimeError(
                f"setup/model-tiers.json tier '{tier}' has invalid model ID '{model_id}'. "
                f"Model IDs must start with one of: {_VALID_MODEL_PREFIXES}."
            )

    return data


def is_local_tier(tier: Tier) -> bool:
    """Whether `tier` currently resolves to a locally-hosted model.

    Call this before sending private user data (task titles, message bodies) to
    a tier whose only justification for seeing that data is that it stays on the
    tailnet. A tier swap in setup/model-tiers.json can point any tier at an
    external provider, so the caller — not the config — has to decide whether
    the data may follow.

    Returns False rather than raising when the tier is unknown or the config
    cannot be read: an unanswerable question about where data goes is answered
    as "not local".
    """
    try:
        model_id = _load_model_tiers()[tier]
    except Exception:
        return False
    return any(model_id.startswith(prefix) for prefix in _LOCAL_MODEL_PREFIXES)


def llm(tier: Tier, *, temperature: float = 0.0, caller: str | None = None) -> ChatOpenAI:
    """Return a LangChain ChatOpenAI instance pointing at the LiteLLM proxy.

    Model IDs are resolved from setup/model-tiers.json, validated at first call.
    LLM_PROXY_BASE_URL must point at the proxy (OpenAI-compatible endpoint, i.e.
    include the /v1 suffix); LLM_PROXY_API_KEY is forwarded as the bearer token.
    Both env vars are required — startup fails if either is unset or empty. If the
    proxy does not require auth, set LLM_PROXY_API_KEY to any non-empty placeholder
    value in the runtime environment.

    An LLMObservabilityCallback is attached automatically, emitting
    llm.call.start / llm.call.end / llm.call.error events to structlog with
    tier, model, caller, duration_ms, and token counts. No message content is
    logged. At-least-once delivery of log events (LangChain callback
    invocation semantics).

    Every returned instance carries an explicit request timeout and retry cap so
    a wedged model host surfaces as a prompt error rather than a hang. Override
    with LLM_REQUEST_TIMEOUT_SECONDS and LLM_MAX_RETRIES; both fall back to the
    module defaults when unset or unparseable.

    Args:
        tier: One of 'expensive', 'medium', 'cheap', 'reminder'.
        temperature: Sampling temperature. Defaults to 0.0 for deterministic output.
        caller: Short string identifying the call site (e.g., "intake", "chat",
            "classify"). Used as a field in log events and to decide whether
            the call reasons (see LLM_REASONING_CALLERS). None is valid for
            callsites that don't pass a caller; they never reason.

    Returns:
        ChatOpenAI configured for the specified tier, with observability
        callback attached.

    Raises:
        RuntimeError: If model-tiers.json is missing or malformed, if
            LANGSMITH_TRACING=true without ALLOW_PRIVATE_TRACE_EXPORT, or if
            LLM_PROXY_BASE_URL or LLM_PROXY_API_KEY is unset or empty.
        ValueError: If tier is not a valid tier name.
    """
    if tier not in _VALID_TIERS:
        raise ValueError(
            f"Unknown model tier '{tier}'. Valid tiers: {sorted(_VALID_TIERS)}"
        )

    tiers = _load_model_tiers()
    model_id = tiers[tier]

    base_url, api_key = _require_llm_proxy_config()

    kwargs: dict[str, Any] = {
        "model": model_id,
        "temperature": temperature,
        "base_url": base_url,
        "api_key": api_key,
        "timeout": _request_timeout_seconds(),
        "max_retries": _max_retries(),
    }
    max_tokens = _TIER_MAX_TOKENS.get(tier)
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    kwargs["extra_body"] = _caller_extra_body(caller)
    base_model = ChatOpenAI(**kwargs)

    # Attach observability callback (one instance per llm() call so tier + caller
    # are baked into the handler and appear as fields on every log event).
    handler = LLMObservabilityCallback(tier=tier, model=model_id, caller=caller)
    return base_model.with_config(callbacks=[handler])  # type: ignore[return-value]


def validate_startup() -> None:
    """Call at application startup to eagerly validate model configuration.

    Raises RuntimeError if setup/model-tiers.json is missing, incomplete, or
    contains invalid model IDs, or if LangSmith guard fires.
    """
    _load_model_tiers()
    _require_llm_proxy_config()
