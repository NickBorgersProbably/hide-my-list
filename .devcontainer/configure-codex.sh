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
# shared LiteLLM proxy deployment used in CI.
CODEX_MODEL_DEFAULT="gpt-5.5"
CODEX_MODEL_PROVIDER_DEFAULT="litellm"
CODEX_MODEL_PROVIDER_NAME_DEFAULT="LiteLLM"
CODEX_MODEL_BASE_URL_DEFAULT="https://llm.featherback-mermaid.ts.net/v1"
CODEX_MODEL_ENV_KEY_DEFAULT="OPENAI_API_KEY"

CODEX_MODEL="${CODEX_MODEL:-$CODEX_MODEL_DEFAULT}"
CODEX_MODEL_PROVIDER="${CODEX_MODEL_PROVIDER:-$CODEX_MODEL_PROVIDER_DEFAULT}"
CODEX_MODEL_PROVIDER_NAME="${CODEX_MODEL_PROVIDER_NAME:-$CODEX_MODEL_PROVIDER_NAME_DEFAULT}"
CODEX_MODEL_BASE_URL="${CODEX_MODEL_BASE_URL:-$CODEX_MODEL_BASE_URL_DEFAULT}"
CODEX_MODEL_ENV_KEY="${CODEX_MODEL_ENV_KEY:-$CODEX_MODEL_ENV_KEY_DEFAULT}"

CODEX_GIT_NAME_DEFAULT="codex[bot]"
CODEX_GIT_EMAIL_DEFAULT="codex[bot]@users.noreply.github.com"
CODEX_GIT_NAME="${CODEX_GIT_NAME:-$CODEX_GIT_NAME_DEFAULT}"
CODEX_GIT_EMAIL="${CODEX_GIT_EMAIL:-$CODEX_GIT_EMAIL_DEFAULT}"

# Write Codex CLI configuration with the selected provider.
cat > "$HOME/.codex/config.toml" <<EOF
model = "${CODEX_MODEL}"
model_provider = "${CODEX_MODEL_PROVIDER}"

[model_providers.${CODEX_MODEL_PROVIDER}]
name = "${CODEX_MODEL_PROVIDER_NAME}"
base_url = "${CODEX_MODEL_BASE_URL}"
env_key = "${CODEX_MODEL_ENV_KEY}"
EOF

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

echo "Codex configured for ${CODEX_MODEL_PROVIDER_NAME} (${CODEX_MODEL_PROVIDER})."
