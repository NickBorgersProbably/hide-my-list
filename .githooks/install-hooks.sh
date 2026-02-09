#!/bin/sh
# Install git hooks by pointing core.hooksPath to this directory.
# This survives devcontainer rebuilds since .git/config persists
# and the hook scripts live in the repository itself.

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "Configuring git to use .githooks/ as hooks directory..."
git -C "$REPO_ROOT" config core.hooksPath .githooks
echo "Git hooks installed successfully."
