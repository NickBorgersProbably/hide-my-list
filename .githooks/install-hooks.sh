#!/usr/bin/env bash
# Install git hooks for the current worktree.
set -euo pipefail

REPO_ROOT=$(git rev-parse --show-toplevel)
git config extensions.worktreeConfig true
git config --worktree core.hooksPath "$REPO_ROOT/.githooks"
echo "Git hooks installed for this worktree (core.hooksPath=$REPO_ROOT/.githooks)"
echo "Re-run this script in each new git worktree because hooksPath is stored per worktree."
