#!/usr/bin/env bash
# Install git hooks by pointing core.hooksPath to .githooks/
set -euo pipefail

REPO_ROOT=$(git rev-parse --show-toplevel)

# core.hooksPath should resolve inside the current worktree.
# Linked worktrees need their own setting after `git worktree add`.
git config extensions.worktreeConfig true
git config --worktree core.hooksPath "$REPO_ROOT/.githooks"

echo "Git hooks installed for this worktree:"
echo "  core.hooksPath = $REPO_ROOT/.githooks"
echo "Re-run bash .githooks/install-hooks.sh after creating a new git worktree."
