#!/usr/bin/env bash
# monitor-pipeline.sh — Poll GitHub for pipeline status changes
# Writes status changes to a monitor file that the agent checks
#
# SECURITY: Only reads from GitHub API. No webhook data processed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SIGNAL_FILE="$SCRIPT_DIR/../.pr-signal"
MONITOR_FILE="$SCRIPT_DIR/../.pipeline-status"
LAST_STATE_FILE="$SCRIPT_DIR/../.pipeline-last-state"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/.github-pat"
REPO="NickBorgersProbably/hide-my-list"
POLL_INTERVAL=120  # seconds

echo "[$(date -Iseconds)] Pipeline monitor started (polling every ${POLL_INTERVAL}s)"

while true; do
    # Check for webhook signal
    if [ -f "$SIGNAL_FILE" ]; then
        SIGNAL_TIME=$(cat "$SIGNAL_FILE")
        rm -f "$SIGNAL_FILE"
        echo "[$(date -Iseconds)] WEBHOOK SIGNAL received (timestamp: $SIGNAL_TIME)" >> "$MONITOR_FILE"
    fi

    # Get latest review run status
    CURRENT_STATE=$(curl -s -H "Authorization: Bearer $GITHUB_PAT" \
        "https://api.github.com/repos/$REPO/actions/workflows/claude-code-review.yml/runs?per_page=1" 2>/dev/null | \
        python3 -c "
import sys,json
d=json.load(sys.stdin)
runs=d.get('workflow_runs',[])
if runs:
    r=runs[0]
    print(f\"{r['id']}|{r['status']}|{r['conclusion'] or 'pending'}|{r['display_title']}\")
else:
    print('none')
" 2>/dev/null || echo "error")

    # Compare with last known state
    LAST_STATE=""
    [ -f "$LAST_STATE_FILE" ] && LAST_STATE=$(cat "$LAST_STATE_FILE")

    if [ "$CURRENT_STATE" != "$LAST_STATE" ] && [ "$CURRENT_STATE" != "error" ]; then
        echo "$CURRENT_STATE" > "$LAST_STATE_FILE"
        
        STATUS=$(echo "$CURRENT_STATE" | cut -d'|' -f2)
        CONCLUSION=$(echo "$CURRENT_STATE" | cut -d'|' -f3)
        TITLE=$(echo "$CURRENT_STATE" | cut -d'|' -f4)
        
        echo "[$(date -Iseconds)] PIPELINE CHANGE: $TITLE — $STATUS/$CONCLUSION" >> "$MONITOR_FILE"
        
        if [ "$STATUS" = "completed" ]; then
            # Count PR comments to see if reviewers posted
            PR_COMMENTS=$(curl -s -H "Authorization: Bearer $GITHUB_PAT" \
                "https://api.github.com/repos/$REPO/pulls?state=open&per_page=1" 2>/dev/null | \
                python3 -c "
import sys,json
prs=json.load(sys.stdin)
if prs:
    pr_num=prs[0]['number']
    print(pr_num)
" 2>/dev/null || echo "")
            
            if [ -n "$PR_COMMENTS" ]; then
                COMMENT_COUNT=$(curl -s -H "Authorization: Bearer $GITHUB_PAT" \
                    "https://api.github.com/repos/$REPO/issues/$PR_COMMENTS/comments?per_page=1" 2>/dev/null | \
                    python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "?")
                echo "[$(date -Iseconds)] PR #$PR_COMMENTS has $COMMENT_COUNT+ comments" >> "$MONITOR_FILE"
            fi
            
            echo "[$(date -Iseconds)] ACTION NEEDED: Review completed — check comments and act" >> "$MONITOR_FILE"
        fi
    fi

    sleep "$POLL_INTERVAL"
done
