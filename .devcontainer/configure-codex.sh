#!/bin/bash

set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

# Codex CLI is baked into the devcontainer image. Verify it's available.
if ! command -v codex &>/dev/null; then
  echo "ERROR: codex not found — expected it baked into the devcontainer image" >&2
  exit 1
fi

mkdir -p "$HOME/.codex"

# Allow the Codex model configuration to be overridden for different LiteLLM
# backends without requiring code changes. These defaults reflect the current
# shared LiteLLM proxy deployment used in CI, and match the "frontier" tier
# in .github/ci/agent-models.env — a caller that never sets CODEX_MODEL still
# lands on the frontier model.
CODEX_MODEL_DEFAULT="gpt-5.6-sol"
CODEX_MODEL_PROVIDER_DEFAULT="litellm"
CODEX_MODEL_PROVIDER_NAME_DEFAULT="LiteLLM"
CODEX_MODEL_BASE_URL_DEFAULT="https://llm.featherback-mermaid.ts.net/v1"
CODEX_MODEL_ENV_KEY_DEFAULT="OPENAI_API_KEY"

CODEX_MODEL="${CODEX_MODEL:-$CODEX_MODEL_DEFAULT}"
CODEX_MODEL_PROVIDER="${CODEX_MODEL_PROVIDER:-$CODEX_MODEL_PROVIDER_DEFAULT}"
CODEX_MODEL_PROVIDER_NAME="${CODEX_MODEL_PROVIDER_NAME:-$CODEX_MODEL_PROVIDER_NAME_DEFAULT}"
CODEX_MODEL_BASE_URL="${CODEX_MODEL_BASE_URL:-$CODEX_MODEL_BASE_URL_DEFAULT}"
CODEX_MODEL_ENV_KEY="${CODEX_MODEL_ENV_KEY:-$CODEX_MODEL_ENV_KEY_DEFAULT}"
# Optional: model_reasoning_effort (minimal|low|medium|high). Unset by
# default so Codex uses its own default rather than silently pinning a
# level; callers that care (see .github/ci/agent-models.env) set
# CODEX_MODEL_REASONING_EFFORT explicitly per role.
CODEX_MODEL_REASONING_EFFORT="${CODEX_MODEL_REASONING_EFFORT:-}"

CODEX_GIT_NAME_DEFAULT="codex[bot]"
CODEX_GIT_EMAIL_DEFAULT="codex[bot]@users.noreply.github.com"
CODEX_GIT_NAME="${CODEX_GIT_NAME:-$CODEX_GIT_NAME_DEFAULT}"
CODEX_GIT_EMAIL="${CODEX_GIT_EMAIL:-$CODEX_GIT_EMAIL_DEFAULT}"

# Write Codex CLI configuration with the selected provider. The reasoning
# effort line is optional and must come before the [model_providers...]
# table header — TOML requires top-level scalar keys ahead of any table.
{
  echo "model = \"${CODEX_MODEL}\""
  echo "model_provider = \"${CODEX_MODEL_PROVIDER}\""
  if [ -n "$CODEX_MODEL_REASONING_EFFORT" ]; then
    echo "model_reasoning_effort = \"${CODEX_MODEL_REASONING_EFFORT}\""
  fi
  echo ""
  echo "[model_providers.${CODEX_MODEL_PROVIDER}]"
  echo "name = \"${CODEX_MODEL_PROVIDER_NAME}\""
  echo "base_url = \"${CODEX_MODEL_BASE_URL}\""
  echo "env_key = \"${CODEX_MODEL_ENV_KEY}\""
} > "$HOME/.codex/config.toml"

# GitHub Actions runs can create commits on PR and issue-resolution branches.
# Pin the author/committer identity there so GitHub attributes those commits to
# the Codex app instead of a runner default or placeholder identity.
if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
  git config --global user.name "${CODEX_GIT_NAME}"
  git config --global user.email "${CODEX_GIT_EMAIL}"

  export GIT_AUTHOR_NAME="${CODEX_GIT_NAME}"
  export GIT_AUTHOR_EMAIL="${CODEX_GIT_EMAIL}"
  export GIT_COMMITTER_NAME="${CODEX_GIT_NAME}"
  export GIT_COMMITTER_EMAIL="${CODEX_GIT_EMAIL}"
fi

echo "Codex configured for ${CODEX_MODEL_PROVIDER_NAME} (${CODEX_MODEL_PROVIDER}), model=${CODEX_MODEL}, reasoning_effort=${CODEX_MODEL_REASONING_EFFORT:-<default>}."
