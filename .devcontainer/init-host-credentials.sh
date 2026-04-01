#!/bin/bash
# Runs on the HOST before the devcontainer starts.
# Extracts credentials from host keychain/CLI and writes them to files
# that postCreateCommand will consume inside the container.
# All token files are gitignored and deleted after use.

set -e
cd "$(dirname "$0")"

# GitHub CLI token
gh auth token > .gh-token 2>/dev/null || true

# Claude Code OAuth credentials
# Try file-based extraction first (Linux), fall back to macOS keychain
CLAUDE_CREDS="$HOME/.claude/.credentials.json"
if [ -f "$CLAUDE_CREDS" ]; then
    cp "$CLAUDE_CREDS" .claude-credentials
    chmod 600 .claude-credentials
elif command -v security &>/dev/null; then
    security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null \
        > .claude-credentials 2>/dev/null || true
fi
