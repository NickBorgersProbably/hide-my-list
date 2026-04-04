#!/usr/bin/env bash
# pull-main.sh — Pull origin/main, signal the agent only if the pull isn't clean
#
# Runs on a 10-minute cron. Clean pulls happen silently. If the working tree
# has uncommitted tracked-file changes or the pull hits a merge conflict, the
# script writes .pull-dirty with enough context for the agent to create a
# GitHub issue preserving the changes before resetting.
#
# SECURITY PROPERTIES:
#   - Signal file contains diffs of tracked files only — no secrets
#   - Never force-pushes or deletes branches
#   - Always exits 0 so the cron system doesn't treat failures as crashes

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

SIGNAL_FILE="$ROOT_DIR/.pull-dirty"
MAX_DIFF_LINES=50

cd "$ROOT_DIR"

# --- Helpers ---

# Build JSON for dirty files using a single python3 invocation.
# Reads file list from stdin (one path per line), collects truncated diffs,
# outputs a JSON array. All escaping handled by python3's json module.
build_dirty_files_json() {
    python3 -c "
import json, subprocess, sys

max_lines = int(sys.argv[1])
files = [line.strip() for line in sys.stdin if line.strip()]
result = []
for path in files:
    try:
        diff = subprocess.run(
            ['git', 'diff', '--', path],
            capture_output=True, text=True, timeout=10
        ).stdout
        lines = diff.split('\n')[:max_lines]
        snippet = '\n'.join(lines)
    except Exception:
        snippet = ''
    result.append({'path': path, 'diff_snippet': snippet})
print(json.dumps(result))
" "$MAX_DIFF_LINES" 2>/dev/null || echo '[]'
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

# --- Main ---

# 1. Check for uncommitted changes to tracked files
dirty_all=$( { git diff --name-only; git diff --cached --name-only; } 2>/dev/null | sort -u | grep -v '^$' || true)

if [ -n "$dirty_all" ]; then
    dirty_json=$(echo "$dirty_all" | build_dirty_files_json)
    write_signal "uncommitted_tracked_changes" "{\"dirty_files\": $dirty_json}"
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

# Abort the failed merge to restore a usable state
git merge --abort 2>/dev/null || true

# Build extra fields for the signal — single python3 call for both values
extra_json=$(PULL_OUTPUT="$pull_output" CONFLICTING="$conflicting" python3 -c "
import json, os
print(json.dumps({
    'error_output': os.environ['PULL_OUTPUT'],
    'conflicting_files': [f for f in os.environ['CONFLICTING'].strip().split('\n') if f],
}))
" 2>/dev/null || echo '{"error_output":"(could not capture)","conflicting_files":[]}')

write_signal "merge_conflict" "$extra_json"
exit 0
