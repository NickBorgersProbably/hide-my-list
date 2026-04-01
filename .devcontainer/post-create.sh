#!/bin/bash
# Post-create setup for devcontainer.
# Called via postCreateCommand in devcontainer.json.
set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

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
