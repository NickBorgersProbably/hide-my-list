#!/usr/bin/env bash
# pull-main.sh — Pull origin/main with script-managed dirty-pull recovery
#
# Runs on a 10-minute cron. Clean pulls happen silently. If the working tree
# has uncommitted tracked-file changes or the pull hits a merge conflict, the
# script creates a GitHub issue preserving the changes, then resets the repo.
# The normal cron path never needs pull-state reasoning. HEARTBEAT only retries
# stale recovery after operator fixes (for example restoring `gh` auth).
#
# SECURITY PROPERTIES:
#   - Signal file contains diffs of tracked files only — no secrets
#   - Never force-pushes or deletes branches
#   - Always exits 0 so the cron system doesn't treat failures as crashes

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck disable=SC1091
# shellcheck source=./load-github-auth.sh
source "$SCRIPT_DIR/load-github-auth.sh"

SIGNAL_FILE="$ROOT_DIR/.pull-dirty"
REPO="NickBorgersProbably/hide-my-list"

cd "$ROOT_DIR"

# --- Helpers ---

# Build JSON for dirty files using a single python3 invocation.
# Reads file list from stdin (one path per line), captures staged and
# unstaged diffs, and outputs a JSON array. All escaping handled by
# python3's json module.
build_dirty_files_json() {
    python3 -c "
import json, subprocess, sys

files = [line.strip() for line in sys.stdin if line.strip()]
result = []
for path in files:
    staged = ''
    worktree = ''
    try:
        staged = subprocess.run(
            ['git', 'diff', '--cached', '--', path],
            capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        staged = ''
    try:
        worktree = subprocess.run(
            ['git', 'diff', '--', path],
            capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        worktree = ''

    parts = []
    staged_clean = staged.strip('\n')
    worktree_clean = worktree.strip('\n')

    if staged_clean:
        parts.append('--- staged diff ---\\n' + staged_clean)

    if worktree_clean and worktree_clean != staged_clean:
        parts.append('--- worktree diff ---\\n' + worktree_clean)

    snippet = '\\n\\n'.join(parts)
    result.append({'path': path, 'diff_snippet': snippet})
print(json.dumps(result))
" 2>/dev/null || echo '[]'
}

# Write the .pull-dirty signal file.
# All values passed via environment to avoid shell injection in heredocs.
write_signal() {
    local reason="$1"
    local extra_json="$2"

    REASON="$reason" \
    HEAD_COMMIT="$(git rev-parse HEAD)" \
    REMOTE_HEAD="$(git ls-remote origin refs/heads/main 2>/dev/null | cut -f1 || echo unknown)" \
    EXTRA_JSON="$extra_json" \
    python3 -c "
import json, os, sys
from datetime import datetime, timezone

signal = {
    'detected_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'reason': os.environ['REASON'],
    'head_commit': os.environ['HEAD_COMMIT'],
    'remote_head': os.environ['REMOTE_HEAD'],
}
extra = json.loads(os.environ['EXTRA_JSON'])
signal.update(extra)
print(json.dumps(signal, indent=2))
" > "$SIGNAL_FILE" 2>/dev/null || echo '{"reason":"'"$reason"'","error":"signal write failed"}' > "$SIGNAL_FILE"
}

# Recover from a dirty pull: create a GitHub issue preserving the changes, then reset.
# Leaves .pull-dirty in place if anything fails so the HEARTBEAT backstop can retry.
recover_dirty_pull() {
    if [ ! -f "$SIGNAL_FILE" ]; then
        return 0
    fi

    # Preflight: gh must be authenticated either via gh auth login or GH_TOKEN
    if ! ensure_github_auth; then
        echo "gh not authenticated and GH_TOKEN unavailable — leaving .pull-dirty for HEARTBEAT backstop" >&2
        return 0
    fi

    # Read the signal file
    local signal_json
    signal_json=$(cat "$SIGNAL_FILE")

    local reason
    reason=$(echo "$signal_json" | python3 -c "import json,sys; print(json.load(sys.stdin).get('reason','unknown'))" 2>/dev/null || echo "unknown")

    # Build the issue body
    local issue_body
    issue_body=$(SIGNAL_JSON="$signal_json" python3 -c "
import json, os

signal = json.loads(os.environ['SIGNAL_JSON'])
reason = signal.get('reason', 'unknown')
head = signal.get('head_commit', 'unknown')
remote = signal.get('remote_head', 'unknown')
detected = signal.get('detected_at', 'unknown')

body = f'''## Dirty pull detected

**Reason:** {reason}
**Detected at:** {detected}
**Local HEAD:** {head}
**Remote HEAD:** {remote}

## Details

'''

if reason == 'uncommitted_tracked_changes':
    files = signal.get('dirty_files', [])
    for f in files:
        body += f\"### {f['path']}\n\"
        snippet = f.get('diff_snippet', '(no diff)')
        body += f\"\`\`\`diff\n{snippet}\n\`\`\`\n\n\"

elif reason == 'merge_conflict':
    body += f\"**Error output:**\n\`\`\`\n{signal.get('error_output', '(none)')}\n\`\`\`\n\n\"
    conflicts = signal.get('conflicting_files', [])
    if conflicts:
        body += '**Conflicting files:**\n'
        for c in conflicts:
            body += f'- {c}\n'
        body += '\n'
    commits = signal.get('local_commits', [])
    if commits:
        body += '**Local commits pending merge:**\n'
        for commit in commits:
            body += f'- {commit}\n'
        body += '\n'
    body += '**Local diff preserved before reset:**\n'
    body += f\"\`\`\`diff\n{signal.get('local_diff', '(no diff captured)')}\n\`\`\`\n\n\"

body += '---\n*Auto-created by pull-main.sh — local changes preserved here before reset.*'

print(body)
" 2>/dev/null) || {
        echo "Failed to build issue body" >&2
        return 0
    }

    # Create the GitHub issue
    local short_reason
    case "$reason" in
        uncommitted_tracked_changes) short_reason="uncommitted local changes" ;;
        merge_conflict) short_reason="merge conflict" ;;
        *) short_reason="$reason" ;;
    esac

    if ! gh issue create \
        --repo "$REPO" \
        --title "Agent local changes need review: $short_reason" \
        --body "$issue_body" 2>/dev/null; then
        echo "Failed to create GitHub issue — leaving .pull-dirty" >&2
        return 0
    fi

    # Reset to match remote
    git checkout -- . 2>/dev/null || true
    git clean -fd 2>/dev/null || true
    if ! git pull origin main 2>/dev/null; then
        echo "Reset pull failed — leaving .pull-dirty" >&2
        return 0
    fi

    # Success — clean up
    rm -f "$SIGNAL_FILE"
}

# --- Flag parsing ---

if [ "${1:-}" = "--recover-only" ]; then
    recover_dirty_pull
    exit 0
fi

# --- Main ---

# 1. Check for uncommitted changes to tracked files
dirty_all=$( { git diff --name-only; git diff --cached --name-only; } 2>/dev/null | sort -u | grep -v '^$' || true)

if [ -n "$dirty_all" ]; then
    dirty_json=$(echo "$dirty_all" | build_dirty_files_json)
    write_signal "uncommitted_tracked_changes" "{\"dirty_files\": $dirty_json}"
    recover_dirty_pull
    exit 0
fi

# 2. Working tree is clean — attempt the pull
pull_output=$(git pull origin main 2>&1) && pull_exit=0 || pull_exit=$?

if [ "$pull_exit" -eq 0 ]; then
    # Clean pull — remove any stale signal from a prior run
    rm -f "$SIGNAL_FILE"
    exit 0
fi

# 3. Pull failed — likely a merge conflict
# Capture conflicting files before aborting
conflicting=$(git diff --name-only --diff-filter=U 2>/dev/null || true)
merge_base=$(git merge-base HEAD origin/main 2>/dev/null || true)
local_commits=""
local_diff=""

if [ -n "$merge_base" ]; then
    local_commits=$(git log --oneline "$merge_base..HEAD" 2>/dev/null || true)
    local_diff=$(git diff "$merge_base..HEAD" 2>/dev/null || true)
fi

# Abort the failed merge to restore a usable state
git merge --abort 2>/dev/null || true

# Build extra fields for the signal — single python3 call for both values
extra_json=$(PULL_OUTPUT="$pull_output" CONFLICTING="$conflicting" LOCAL_COMMITS="$local_commits" LOCAL_DIFF="$local_diff" python3 -c "
import json, os
print(json.dumps({
    'error_output': os.environ['PULL_OUTPUT'],
    'conflicting_files': [f for f in os.environ['CONFLICTING'].strip().split('\n') if f],
    'local_commits': [c for c in os.environ['LOCAL_COMMITS'].strip().split('\n') if c],
    'local_diff': os.environ['LOCAL_DIFF'],
}))
" 2>/dev/null || echo '{"error_output":"(could not capture)","conflicting_files":[]}')

write_signal "merge_conflict" "$extra_json"
recover_dirty_pull
exit 0
