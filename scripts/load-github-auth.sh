#!/usr/bin/env bash
# load-github-auth.sh — export GH_TOKEN from repo .env when gh is not already authenticated

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

load_github_pat_from_env() {
    (
        # load-env.sh scopes the shell to the requested key and honors
        # HIDE_MY_LIST_ENV_FILE when operators override the env file path.
        # shellcheck disable=SC1091
        source "$SCRIPT_DIR/load-env.sh" GITHUB_PAT? || exit 0
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
