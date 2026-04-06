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
legacy_workflow_line="- **Workflow**: ${workflow_name}"
legacy_branch_line="- **Branch**: ${head_branch}"
legacy_commit_line="- **Commit**: ${head_sha}"

# shellcheck disable=SC2016
match_filter='
  [
    .[]
    | select(
        (.body // "" | contains($marker))
        or (.body // "" | contains($run_url))
        or .title == $title
        or (
          (.body // "" | contains($legacy_workflow_line))
          and (.body // "" | contains($legacy_branch_line))
          and (.body // "" | contains($legacy_commit_line))
        )
      )
  ]
  | sort_by(.updatedAt)
  | last
  | .number // empty
'

find_matching_issue() {
    local issue_state="$1"
    gh issue list \
        --repo "$repo" \
        --label github-actions \
        --state "$issue_state" \
        --limit 200 \
        --json number,title,body,updatedAt |
        jq -r \
            --arg marker "$marker" \
            --arg run_url "$run_url" \
            --arg title "$title" \
            --arg legacy_workflow_line "$legacy_workflow_line" \
            --arg legacy_branch_line "$legacy_branch_line" \
            --arg legacy_commit_line "$legacy_commit_line" \
            "$match_filter"
}

open_issue=$(find_matching_issue open)

if [ -n "$open_issue" ]; then
    echo "$open_issue"
    exit 0
fi

if [ "$check_only" = "true" ]; then
    exit 0
fi

closed_issue=$(find_matching_issue closed)

gh label create "github-actions" \
    --color "0366d6" \
    --description "GitHub Actions workflow issues" \
    --repo "$repo" \
    2>/dev/null || true

issue_body=$(cat "$body_file")
if [[ "$issue_body" != *"$marker"* ]]; then
    issue_body="${issue_body}"$'\n\n'"${marker}"
fi

if [ -n "$closed_issue" ]; then
    gh issue reopen "$closed_issue" --repo "$repo" >/dev/null
    gh issue edit "$closed_issue" \
        --repo "$repo" \
        --title "$title" \
        --body "$issue_body" \
        --add-label "bug" \
        --add-label "github-actions" \
        --add-assignee "NickBorgers" >/dev/null
    echo "$closed_issue"
    exit 0
fi

gh issue create \
    --repo "$repo" \
    --title "$title" \
    --assignee NickBorgers \
    --label "bug,github-actions" \
    --body "$issue_body"
