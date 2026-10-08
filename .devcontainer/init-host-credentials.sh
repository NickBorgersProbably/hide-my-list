#!/bin/bash
# Runs on the HOST before the devcontainer starts.
# Extracts credentials from host keychain/CLI and writes them to files
# that postCreateCommand will consume inside the container.
# All token files are gitignored. .gh-token is deleted after use;
# .claude-oauth-token stays, because every container shell reads it.

set -e
cd "$(dirname "$0")"

# Ensure every devcontainer.json bind-mount source exists before Docker
# tries to resolve it. Missing bind-mount sources are dangerous: Docker
# silently materializes them as root-owned directories on the host (on
# Linux) or errors out at container start (on macOS / Docker Desktop).
# Creating user-owned placeholders here is a cheap no-op when the files
# already exist, and guarantees the devcontainer can spin up on any
# contributor's machine. post-create.sh uses `-s` (non-empty) guards
# before wiring anything in, so empty placeholders are installed but
# never activated.

[ -e "$HOME/.claude"           ] || mkdir -p "$HOME/.claude"
[ -e "$HOME/.claude/projects"  ] || mkdir -p "$HOME/.claude/projects"
[ -e "$HOME/.gitconfig"        ] || touch    "$HOME/.gitconfig"
[ -e "$HOME/.claude.json"      ] || touch    "$HOME/.claude.json"
[ -e "$HOME/.bashrc"           ] || touch    "$HOME/.bashrc"
[ -d "$HOME/code/util"         ] || mkdir -p "$HOME/code/util"
[ -e "$HOME/code/util/profile" ] || touch    "$HOME/code/util/profile"

# GitHub CLI token
gh auth token > .gh-token 2>/dev/null || true

# Claude Code OAuth access token.
#
# Only the short-lived ACCESS token leaves the host. Claude's refresh token
# rotates on use, so a container holding it can refresh and invalidate the
# host's login (and every other session). The container exports the access
# token as CLAUDE_CODE_OAUTH_TOKEN (see wire-claude-token.sh); it cannot
# refresh anything. The file is rewritten in place on every start so the
# bind-mounted workspace always sees the current token.
CLAUDE_TOKEN_FILE=".claude-oauth-token"
rm -f .claude-credentials   # earlier revisions copied the whole file here

claude_creds_json() {
    # Linux keeps the login in a file; macOS keeps it in the keychain.
    if [ -f "$HOME/.claude/.credentials.json" ]; then
        cat "$HOME/.claude/.credentials.json"
    elif command -v security &>/dev/null; then
        security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null
    fi
}

claude_json_field() {
    # $1 = field under claudeAiOauth; JSON on stdin; prints nothing on failure.
    python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("claudeAiOauth", {}).get(sys.argv[1], ""))
except Exception:
    pass
' "$1" 2>/dev/null
}

claude_json="$(claude_creds_json || true)"
claude_token="$(printf '%s' "$claude_json" | claude_json_field accessToken)"
claude_expires_ms="$(printf '%s' "$claude_json" | claude_json_field expiresAt)"
unset claude_json

: > "$CLAUDE_TOKEN_FILE"    # truncate in place; stale tokens never survive
chmod 600 "$CLAUDE_TOKEN_FILE"
now_ms=$(( $(date +%s) * 1000 ))
if [ -z "$claude_token" ]; then
    echo "[init-host-credentials] No Claude login on the host; the container will need its own login." >&2
elif [ -n "$claude_expires_ms" ] && [ "$claude_expires_ms" -le "$now_ms" ] 2>/dev/null; then
    echo "[init-host-credentials] The host Claude token has expired; run claude on the host, then restart the devcontainer." >&2
else
    printf '%s\n' "$claude_token" > "$CLAUDE_TOKEN_FILE"
    echo "[init-host-credentials] Claude access token captured (no refresh token)." >&2
fi
unset claude_token
