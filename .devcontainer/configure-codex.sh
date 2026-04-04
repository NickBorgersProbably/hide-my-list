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
CODEX_MODEL_DEFAULT="gpt-5.4"
CODEX_SUPPORTED_MODELS_DEFAULT="gpt-5.4"
CODEX_MODEL_PROVIDER_DEFAULT="litellm"
CODEX_MODEL_PROVIDER_NAME_DEFAULT="LiteLLM"
CODEX_MODEL_BASE_URL_DEFAULT="https://llm.featherback-mermaid.ts.net/v1"
CODEX_MODEL_ENV_KEY_DEFAULT="OPENAI_API_KEY"

CODEX_MODEL="${CODEX_MODEL:-$CODEX_MODEL_DEFAULT}"
CODEX_SUPPORTED_MODELS="${CODEX_SUPPORTED_MODELS:-$CODEX_SUPPORTED_MODELS_DEFAULT}"
CODEX_MODEL_PROVIDER="${CODEX_MODEL_PROVIDER:-$CODEX_MODEL_PROVIDER_DEFAULT}"
CODEX_MODEL_PROVIDER_NAME="${CODEX_MODEL_PROVIDER_NAME:-$CODEX_MODEL_PROVIDER_NAME_DEFAULT}"
CODEX_MODEL_BASE_URL="${CODEX_MODEL_BASE_URL:-$CODEX_MODEL_BASE_URL_DEFAULT}"
CODEX_MODEL_ENV_KEY="${CODEX_MODEL_ENV_KEY:-$CODEX_MODEL_ENV_KEY_DEFAULT}"

case " $CODEX_SUPPORTED_MODELS " in
  *" $CODEX_MODEL "*) ;;
  *)
    echo "ERROR: Unsupported Codex model \"$CODEX_MODEL\". Allowed models for this environment: $CODEX_SUPPORTED_MODELS" >&2
    exit 1
    ;;
esac

export CODEX_MODEL
export CODEX_SUPPORTED_MODELS
export CODEX_MODEL_PROVIDER
export CODEX_MODEL_PROVIDER_NAME
export CODEX_MODEL_BASE_URL
export CODEX_MODEL_ENV_KEY

codex_with_trusted_config() {
  codex \
    -c "model=\"$CODEX_MODEL\"" \
    -c "model_provider=\"$CODEX_MODEL_PROVIDER\"" \
    -c "model_providers.${CODEX_MODEL_PROVIDER}.name=\"$CODEX_MODEL_PROVIDER_NAME\"" \
    -c "model_providers.${CODEX_MODEL_PROVIDER}.base_url=\"$CODEX_MODEL_BASE_URL\"" \
    -c "model_providers.${CODEX_MODEL_PROVIDER}.env_key=\"$CODEX_MODEL_ENV_KEY\"" \
    "$@"
}

# Write Codex CLI configuration with the selected provider.
cat > "$HOME/.codex/config.toml" <<EOF
model = "${CODEX_MODEL}"
model_provider = "${CODEX_MODEL_PROVIDER}"

[model_providers.${CODEX_MODEL_PROVIDER}]
name = "${CODEX_MODEL_PROVIDER_NAME}"
base_url = "${CODEX_MODEL_BASE_URL}"
env_key = "${CODEX_MODEL_ENV_KEY}"
EOF

echo "Codex configured for ${CODEX_MODEL_PROVIDER_NAME} (${CODEX_MODEL_PROVIDER})."
