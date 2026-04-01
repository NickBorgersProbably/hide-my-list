#!/bin/bash
# Post-create setup for devcontainer.
# Called via postCreateCommand in devcontainer.json.
set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

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
