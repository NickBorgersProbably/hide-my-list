#!/usr/bin/env bash
# check-github-status.sh — Check GitHub for PR reviews, workflow failures, and issues
# Called by the agent when the webhook signal fires
# Outputs structured status for the agent to process
#
# SECURITY: This script only reads from GitHub's public API.
# No webhook request data is processed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/../.env"

REPO="NickBorgersProbably/hide-my-list"
# PAT is optional — public repos work without auth, but rate limits are higher with it
AUTH_HEADER=""
if [ -n "${GITHUB_PAT:-}" ]; then
    AUTH_HEADER="Authorization: Bearer $GITHUB_PAT"
fi

curl_gh() {
    if [ -n "$AUTH_HEADER" ]; then
        curl -s -H "$AUTH_HEADER" "$@"
    else
        curl -s "$@"
    fi
}

echo "=== Open PRs ==="
curl_gh "https://api.github.com/repos/$REPO/pulls?state=open&per_page=10" | python3 -c "
import sys,json
prs=json.load(sys.stdin)
for pr in prs:
    print(f\"PR #{pr['number']}: {pr['title']} ({pr['head']['ref']})\")
    print(f\"  Labels: {[l['name'] for l in pr.get('labels',[])]}\")
" 2>/dev/null || echo "Failed to fetch PRs"

echo ""
echo "=== Recent workflow runs ==="
curl_gh "https://api.github.com/repos/$REPO/actions/runs?per_page=10" | python3 -c "
import sys,json
d=json.load(sys.stdin)
for r in d.get('workflow_runs',[])[:10]:
    status = f\"{r['status']}/{r['conclusion'] or 'pending'}\"
    print(f\"  {r['name']} ({r['head_branch']}): {status}\")
" 2>/dev/null || echo "Failed to fetch runs"

echo ""
echo "=== Open issues ==="
curl_gh "https://api.github.com/repos/$REPO/issues?state=open&per_page=10&labels=github-actions" | python3 -c "
import sys,json
issues=json.load(sys.stdin)
if not issues:
    print('  No open github-actions issues')
else:
    for i in issues:
        print(f\"  Issue #{i['number']}: {i['title']}\")
" 2>/dev/null || echo "Failed to fetch issues"

echo ""
echo "=== PR review comments (most recent open PR) ==="
PR_NUMBER=$(curl_gh "https://api.github.com/repos/$REPO/pulls?state=open&per_page=1" | python3 -c "
import sys,json
prs=json.load(sys.stdin)
print(prs[0]['number'] if prs else '')
" 2>/dev/null)

if [ -n "$PR_NUMBER" ]; then
    curl_gh "https://api.github.com/repos/$REPO/issues/$PR_NUMBER/comments?per_page=20" | python3 -c "
import sys,json
comments=json.load(sys.stdin)
print(f'  {len(comments)} comments on PR #{int(sys.argv[1])}')
for c in comments[-5:]:
    user = c['user']['login']
    body = c['body'][:200].replace(chr(10),' ')
    print(f\"  [{user}] {body}\")
" "$PR_NUMBER" 2>/dev/null || echo "Failed to fetch comments"
fi
