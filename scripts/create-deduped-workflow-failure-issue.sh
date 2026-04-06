#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF' >&2
Usage:
  create-deduped-workflow-failure-issue.sh --check-only <repo> <workflow_name> <run_id> <run_url> <head_branch> <head_sha>
  create-deduped-workflow-failure-issue.sh <repo> <workflow_name> <run_id> <run_url> <head_branch> <head_sha> <body_file>
EOF
    exit 1
}

check_only="false"
if [ "${1:-}" = "--check-only" ]; then
    check_only="true"
    shift
fi

if [ "$check_only" = "true" ]; then
    [ "$#" -eq 6 ] || usage
else
    [ "$#" -eq 7 ] || usage
fi

repo="$1"
workflow_name="$2"
run_id="$3"
run_url="$4"
head_branch="$5"
head_sha="$6"
body_file="${7:-}"

if [ "$check_only" != "true" ] && [ ! -f "$body_file" ]; then
    echo "Body file not found: $body_file" >&2
    exit 1
fi

fingerprint=$(printf '%s\n%s\n%s\n' "$workflow_name" "$head_branch" "$head_sha" | sha256sum | awk '{print $1}')
marker="<!-- codex-workflow-failure-fingerprint:${fingerprint} -->"
title="Workflow Failure: ${workflow_name} run #${run_id} - Actions Configuration Issue"

existing_issue=$(
    # shellcheck disable=SC2016
    gh issue list \
        --repo "$repo" \
        --label github-actions \
        --state all \
        --limit 200 \
        --json number,title,body \
        --jq --arg marker "$marker" --arg run_url "$run_url" --arg title "$title" '
          [.[] | select((.body // "" | contains($marker)) or (.body // "" | contains($run_url)) or .title == $title)][0].number // empty
        '
)

if [ -n "$existing_issue" ]; then
    echo "$existing_issue"
    exit 0
fi

if [ "$check_only" = "true" ]; then
    exit 0
fi

gh label create "github-actions" \
    --color "0366d6" \
    --description "GitHub Actions workflow issues" \
    --repo "$repo" \
    2>/dev/null || true

issue_body=$(cat "$body_file")
if [[ "$issue_body" != *"$marker"* ]]; then
    issue_body="${issue_body}"$'\n\n'"${marker}"
fi

gh issue create \
    --repo "$repo" \
    --title "$title" \
    --assignee NickBorgers \
    --label "bug,github-actions" \
    --body "$issue_body"
