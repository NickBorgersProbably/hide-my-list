#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEVCONTAINER_DOCKERFILE="$SCRIPT_DIR/Dockerfile"

get_arg_version() {
  local arg_name=$1
  local value
  value=$(grep -oP "ARG ${arg_name}=\\K[^[:space:]]+" "$DEVCONTAINER_DOCKERFILE" | tail -n 1 || true)
  if [ -z "$value" ]; then
    echo "Failed to read ${arg_name} from ${DEVCONTAINER_DOCKERFILE}" >&2
    exit 1
  fi
  printf '%s\n' "$value"
}

CODEX_CLI_VERSION="$(get_arg_version CODEX_CLI_VERSION)"

export PATH="$HOME/.local/bin:$PATH"

# Fallback: install codex if not already present (e.g., if post-create.sh
# was skipped or if devcontainer features wiped the install).
if ! command -v codex &>/dev/null; then
  echo "codex not found, installing..."
  curl -fsSL --retry 5 --retry-delay 2 --retry-connrefused "https://github.com/openai/codex/releases/download/rust-v${CODEX_CLI_VERSION}/install.sh" \
    | sh -s -- "${CODEX_CLI_VERSION}"
fi

mkdir -p "$HOME/.codex"

# Allow the Codex model configuration to be overridden for different LiteLLM
# backends without requiring code changes. These defaults reflect the current
# shared LiteLLM proxy deployment used in CI.
CODEX_MODEL_DEFAULT="gpt-5.4"
CODEX_MODEL_PROVIDER_DEFAULT="litellm"
CODEX_MODEL_PROVIDER_NAME_DEFAULT="LiteLLM"
CODEX_MODEL_BASE_URL_DEFAULT="https://llm.featherback-mermaid.ts.net/v1"
CODEX_MODEL_ENV_KEY_DEFAULT="OPENAI_API_KEY"

CODEX_MODEL="${CODEX_MODEL:-$CODEX_MODEL_DEFAULT}"
CODEX_MODEL_PROVIDER="${CODEX_MODEL_PROVIDER:-$CODEX_MODEL_PROVIDER_DEFAULT}"
CODEX_MODEL_PROVIDER_NAME="${CODEX_MODEL_PROVIDER_NAME:-$CODEX_MODEL_PROVIDER_NAME_DEFAULT}"
CODEX_MODEL_BASE_URL="${CODEX_MODEL_BASE_URL:-$CODEX_MODEL_BASE_URL_DEFAULT}"
CODEX_MODEL_ENV_KEY="${CODEX_MODEL_ENV_KEY:-$CODEX_MODEL_ENV_KEY_DEFAULT}"

# Write Codex CLI configuration with the selected provider.
cat > "$HOME/.codex/config.toml" <<EOF
model = "${CODEX_MODEL}"
model_provider = "${CODEX_MODEL_PROVIDER}"

[model_providers.${CODEX_MODEL_PROVIDER}]
name = "${CODEX_MODEL_PROVIDER_NAME}"
base_url = "${CODEX_MODEL_BASE_URL}"
env_key = "${CODEX_MODEL_ENV_KEY}"
EOF

echo "Codex configured for LiteLLM proxy."
