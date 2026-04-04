#!/bin/bash
# Post-create setup for devcontainer.
# Called via postCreateCommand in devcontainer.json.
set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Setting up devcontainer ==="

# Configure tmux to use xterm-256color so CLI tools (e.g. Claude Code) render correctly
cat > "$HOME/.tmux.conf" << 'TMUXEOF'
set -g default-terminal "xterm-256color"
set-environment -g LANG en_US.UTF-8
TMUXEOF

# Set up gh CLI credentials from host token (written by initializeCommand)
REPO_TOKEN_FILE="$REPO_ROOT/.devcontainer/.gh-token"
if [ -s "$REPO_TOKEN_FILE" ]; then
  echo "Setting up GitHub CLI credentials..."
  GH_TOKEN=$(cat "$REPO_TOKEN_FILE")
  echo "$GH_TOKEN" | gh auth login --with-token 2>/dev/null &&
    echo "gh auth configured." || echo "Warning: gh auth login failed."
  rm -f "$REPO_TOKEN_FILE"
else
  echo "Warning: No gh token found; gh CLI credentials not available."
fi

# Set up Claude Code credentials from host keychain (written by initializeCommand)
# Claude Code reads ~/.claude/.credentials.json on Linux (no keychain available)
CLAUDE_CRED_FILE="$REPO_ROOT/.devcontainer/.claude-credentials"
if [ -s "$CLAUDE_CRED_FILE" ]; then
  echo "Setting up Claude Code credentials..."
  mkdir -p "$HOME/.claude"
  cp "$CLAUDE_CRED_FILE" "$HOME/.claude/.credentials.json"
  chmod 600 "$HOME/.claude/.credentials.json"
  echo "Claude Code credentials configured."
  rm -f "$CLAUDE_CRED_FILE"
else
  echo "Warning: No Claude credentials found; Claude Code credentials not available."
fi

# Merge host Claude Code config into container config (written by initializeCommand)
# The container .claude.json has hasCompletedOnboarding from the Dockerfile;
# the host .claude.json has oauthAccount info.
CLAUDE_CONFIG_FILE="$REPO_ROOT/.devcontainer/.claude-config"
if [ -s "$CLAUDE_CONFIG_FILE" ]; then
  echo "Merging Claude Code config from host..."
  EXISTING_CONFIG="$HOME/.claude.json"
  if [ -s "$EXISTING_CONFIG" ]; then
    python3 -c "
import json, sys
host = json.load(open(sys.argv[1]))
container = json.load(open(sys.argv[2]))
host.update(container)
json.dump(host, open(sys.argv[2], 'w'), indent=2)
" "$CLAUDE_CONFIG_FILE" "$EXISTING_CONFIG"
  else
    cp "$CLAUDE_CONFIG_FILE" "$EXISTING_CONFIG"
  fi
  chmod 600 "$EXISTING_CONFIG"
  echo "Claude Code config merged."
  rm -f "$CLAUDE_CONFIG_FILE"
else
  echo "Warning: No Claude config found; skipping config merge."
fi

# Helper to read pinned versions from the Dockerfile (single source of truth)
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

export PATH="$HOME/.local/bin:$PATH"

# Codex CLI is baked into the devcontainer image.
# Verify the expected version is present.
CODEX_CLI_VERSION="$(get_arg_version CODEX_CLI_VERSION)"
CURRENT_CODEX_VERSION="$(codex --version 2>/dev/null | awk '{print $2}' || true)"
if [ "$CURRENT_CODEX_VERSION" != "$CODEX_CLI_VERSION" ]; then
    echo "Warning: Codex CLI version mismatch (have: ${CURRENT_CODEX_VERSION}, want: ${CODEX_CLI_VERSION})"
    echo "Rebuilding the devcontainer image should fix this."
fi

bash "$SCRIPT_DIR/configure-codex.sh"

echo "=== Devcontainer setup complete ==="
