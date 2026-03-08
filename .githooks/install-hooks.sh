#!/usr/bin/env bash
# Install git hooks by pointing core.hooksPath to .githooks/
set -euo pipefail

REPO_ROOT=$(git rev-parse --show-toplevel)
git config core.hooksPath "$REPO_ROOT/.githooks"
echo "Git hooks installed (core.hooksPath set to .githooks/)"
