#!/usr/bin/env bash
# load-github-auth.sh — export GH_TOKEN from repo .env when gh is not already authenticated

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"

load_github_pat_from_env() {
    if [ ! -f "$ENV_FILE" ]; then
        return 1
    fi

    (
        set +u
        set -a
        # shellcheck source=/dev/null
        . "$ENV_FILE" >/dev/null 2>&1 || exit 0
        printf '%s' "${GITHUB_PAT:-}"
    )
}

gh_can_auth() {
    gh api user >/dev/null 2>&1
}

ensure_github_auth() {
    if gh_can_auth; then
        return 0
    fi

    local github_pat=""
    github_pat="$(load_github_pat_from_env || true)"
    if [ -n "$github_pat" ]; then
        export GH_TOKEN="$github_pat"
    fi

    gh_can_auth
}
