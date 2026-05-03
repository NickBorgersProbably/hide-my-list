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
CONFIG_DRIFT_FILE="$ROOT_DIR/.config-drift"
TEMPLATE_FILE="$ROOT_DIR/setup/openclaw.json.template"
REPO="NickBorgersProbably/hide-my-list"

# Hash the current on-disk template, or empty string if missing.
template_hash() {
    if [ -f "$TEMPLATE_FILE" ]; then
        git hash-object "$TEMPLATE_FILE" 2>/dev/null || echo ""
    else
        echo ""
    fi
}

# Write .config-drift flag if the template changed across a pull.
# Writes AFTER git operations complete, so recovery-path `git clean -fd`
# cannot delete an unconsumed flag — that was the blocker that closed PR #395.
maybe_write_config_drift() {
    local pre="$1" post="$2"
    if [ -n "$post" ] && [ "$pre" != "$post" ]; then
        DETECTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        PRE_SHA="$pre" \
        POST_SHA="$post" \
        python3 -c "
import json, os
print(json.dumps({
    'detected_at': os.environ['DETECTED_AT'],
    'pre_template_sha': os.environ['PRE_SHA'],
    'post_template_sha': os.environ['POST_SHA'],
}, indent=2))
" > "$CONFIG_DRIFT_FILE" 2>/dev/null || true
    fi
}

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

# Distinguish real merge conflicts from transport/auth/etc. pull failures.
# Dirty-pull recovery only applies when there is local state to preserve before
# resetting the repo; clean-worktree pull errors should leave the checkout alone.
pull_failure_is_merge_conflict() {
    local pull_output="$1"
    local conflicting="$2"

    if [ -n "$conflicting" ]; then
        return 0
    fi

    if git rev-parse -q --verify MERGE_HEAD >/dev/null 2>&1; then
        return 0
    fi

    if printf '%s\n' "$pull_output" | grep -Eq '(^|[[:space:]])CONFLICT([[:space:]]|:)|Automatic merge failed'; then
        return 0
    fi

    return 1
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
import json, os, re, subprocess
from pathlib import Path


def git_show_head(path):
    try:
        return subprocess.run(
            ['git', 'show', f'HEAD:{path}'],
            capture_output=True, text=True, check=True, timeout=10
        ).stdout
    except Exception:
        return ''


def extract_cron_model(text):
    match = re.search(r'^\s+model:\s+(litellm/[A-Za-z0-9._-]+)', text, re.MULTILINE)
    return match.group(1) if match else ''


def extract_cheap_tier(tier_text):
    try:
        return json.loads(tier_text).get('cheap', '')
    except Exception:
        return ''

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
    dirty_paths = {f.get('path', '') for f in files}
    for f in files:
        body += f\"### {f['path']}\n\"
        snippet = f.get('diff_snippet', '(no diff)')
        body += f\"\`\`\`diff\n{snippet}\n\`\`\`\n\n\"

    if dirty_paths & {
        'setup/cron/pull-main.md',
        'setup/cron/reminder-check.md',
        'setup/model-tiers.json',
        'setup/openclaw.json.template',
    }:
        head_tiers = git_show_head('setup/model-tiers.json')
        head_cheap_tier = extract_cheap_tier(head_tiers)
        head_pull_model = extract_cron_model(git_show_head('setup/cron/pull-main.md'))
        head_reminder_model = extract_cron_model(git_show_head('setup/cron/reminder-check.md'))
        dirty_models = []

        for path in (
            'setup/cron/pull-main.md',
            'setup/cron/reminder-check.md',
        ):
            if path not in dirty_paths:
                continue
            try:
                worktree_model = extract_cron_model(Path(path).read_text())
            except Exception:
                worktree_model = ''
            if worktree_model:
                dirty_models.append((path, worktree_model))

        body += '## Canonical repo contract at HEAD\n\n'
        if head_cheap_tier:
            body += f'- setup/model-tiers.json cheap: {head_cheap_tier}\n'
        if head_pull_model:
            body += f'- setup/cron/pull-main.md: {head_pull_model}\n'
        if head_reminder_model:
            body += f'- setup/cron/reminder-check.md: {head_reminder_model}\n'
        if dirty_models:
            body += '\n**Dirty worktree values captured before reset:**\n'
            for path, model in dirty_models:
                body += f'- {path}: {model}\n'
        if head_cheap_tier:
            body += '\nReview note: cron-spec model changes should normally stay aligned with setup/model-tiers.json cheap.\n\n'

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
    maybe_write_config_drift "$PRE_TEMPLATE_HASH" "$(template_hash)"
}

# --- Flag parsing ---

# Capture template hash before any git operation so both paths can detect
# whether the pull changed setup/openclaw.json.template.
PRE_TEMPLATE_HASH="$(template_hash)"
export PRE_TEMPLATE_HASH

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
    maybe_write_config_drift "$PRE_TEMPLATE_HASH" "$(template_hash)"
    exit 0
fi

# 3. Pull failed — only recover if it is an actual merge conflict
# Capture conflicting files before deciding whether dirty-pull recovery applies.
conflicting=$(git diff --name-only --diff-filter=U 2>/dev/null || true)

if ! pull_failure_is_merge_conflict "$pull_output" "$conflicting"; then
    printf '%s\n%s\n' \
        "git pull failed without merge-conflict markers; leaving workspace untouched" \
        "$pull_output" >&2
    exit 0
fi

merge_base=$(git merge-base HEAD origin/main 2>/dev/null || true)
local_commits=""
local_diff=""

if [ -n "$merge_base" ]; then
    local_commits=$(git log --oneline "$merge_base..HEAD" 2>/dev/null || true)
    local_diff=$(git diff "$merge_base..HEAD" 2>/dev/null || true)
fi

# Abort the failed merge to restore a usable state
if git rev-parse -q --verify MERGE_HEAD >/dev/null 2>&1; then
    git merge --abort 2>/dev/null || true
fi

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
