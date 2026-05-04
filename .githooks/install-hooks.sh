#!/usr/bin/env bash
# Install git hooks for the current worktree.
#
# Devs: run this once per worktree to point core.hooksPath at .githooks.
# CI: passes core.hooksPath through GIT_CONFIG_* env vars instead, so this
# script becomes a no-op if .git/config is not writable (typical for the
# bind-mounted runner workspace inside an agent container running as a
# different UID).
set -euo pipefail

REPO_ROOT=$(git rev-parse --show-toplevel)
GIT_DIR=$(git rev-parse --git-dir)
CONFIG_FILE="$GIT_DIR/config"

if [ ! -w "$CONFIG_FILE" ]; then
  echo "install-hooks.sh: $CONFIG_FILE not writable; skipping git config writes."
  echo "If hooks should be active here, set core.hooksPath via env vars (GIT_CONFIG_COUNT/GIT_CONFIG_KEY_n=core.hooksPath/GIT_CONFIG_VALUE_n=$REPO_ROOT/.githooks) or run this installer as a user with write access to $CONFIG_FILE."
  exit 0
fi

git config extensions.worktreeConfig true
git config --worktree core.hooksPath "$REPO_ROOT/.githooks"
echo "Git hooks installed for this worktree (core.hooksPath=$REPO_ROOT/.githooks)"
echo "Re-run this script in each new git worktree because hooksPath is stored per worktree."
