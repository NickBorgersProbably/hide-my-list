#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  cleanup-stale-pr-branches.sh [--repo owner/repo] [--delete] [branch ...]
  cleanup-stale-pr-branches.sh [--repo owner/repo] [--delete] --all-closed-prs

Safely deletes remote branches from closed pull requests after verifying:
- the branch still exists on origin
- the branch is not the repository default branch
- the branch does not have an open pull request
- the branch has at least one closed pull request

By default this script performs a dry run. Pass --delete to actually remove refs.
EOF
}

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "ERROR: Missing required command: $1" >&2
        exit 1
    fi
}

api_encode() {
    jq -rn --arg value "$1" '$value | @uri'
}

DELETE=false
ALL_CLOSED_PRS=false
REPO="${GITHUB_REPOSITORY:-}"
declare -a REQUESTED_BRANCHES=()

while [ $# -gt 0 ]; do
    case "$1" in
        --delete)
            DELETE=true
            ;;
        --all-closed-prs)
            ALL_CLOSED_PRS=true
            ;;
        --repo)
            shift
            if [ $# -eq 0 ]; then
                echo "ERROR: --repo requires an owner/repo value" >&2
                exit 1
            fi
            REPO="$1"
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            while [ $# -gt 0 ]; do
                REQUESTED_BRANCHES+=("$1")
                shift
            done
            break
            ;;
        -*)
            echo "ERROR: Unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
        *)
            REQUESTED_BRANCHES+=("$1")
            ;;
    esac
    shift
done

require_command gh
require_command jq

if [ -z "$REPO" ]; then
    REPO="$(gh repo view --json nameWithOwner --jq '.nameWithOwner')"
fi

DEFAULT_BRANCH="$(gh repo view "$REPO" --json defaultBranchRef --jq '.defaultBranchRef.name')"

branch_exists_in_repo() {
    local encoded_branch

    encoded_branch="$(api_encode "$1")"
    gh api "repos/$REPO/branches/$encoded_branch" >/dev/null 2>&1
}

list_same_repo_prs_for_branch() {
    local state="$1"
    local branch="$2"

    gh pr list \
        --repo "$REPO" \
        --state "$state" \
        --head "$branch" \
        --limit 100 \
        --json number,title,mergedAt,closedAt,url,isCrossRepository \
        --jq '.[] | select(.isCrossRepository == false)'
}

collect_all_closed_pr_branches() {
    gh pr list \
        --repo "$REPO" \
        --state closed \
        --limit 1000 \
        --json headRefName,isCrossRepository \
        --jq '.[] | select(.isCrossRepository == false and (.headRefName // "") != "") | .headRefName' |
        sort -u
}

if [ "$ALL_CLOSED_PRS" = true ]; then
    mapfile -t DISCOVERED_BRANCHES < <(collect_all_closed_pr_branches)
    REQUESTED_BRANCHES+=("${DISCOVERED_BRANCHES[@]}")
fi

if [ "${#REQUESTED_BRANCHES[@]}" -eq 0 ]; then
    echo "ERROR: Provide at least one branch or pass --all-closed-prs" >&2
    usage >&2
    exit 1
fi

declare -A SEEN_BRANCHES=()
declare -a BRANCHES=()

for branch in "${REQUESTED_BRANCHES[@]}"; do
    if [ -z "$branch" ]; then
        continue
    fi

    if [ -n "${SEEN_BRANCHES[$branch]:-}" ]; then
        continue
    fi

    SEEN_BRANCHES["$branch"]=1
    BRANCHES+=("$branch")
done

deleted_count=0
skipped_count=0

for branch in "${BRANCHES[@]}"; do
    if [ "$branch" = "$DEFAULT_BRANCH" ]; then
        echo "SKIP  $branch: default branch"
        skipped_count=$((skipped_count + 1))
        continue
    fi

    if ! branch_exists_in_repo "$branch"; then
        echo "SKIP  $branch: branch does not exist in $REPO"
        skipped_count=$((skipped_count + 1))
        continue
    fi

    open_pr_count="$(list_same_repo_prs_for_branch open "$branch" | jq -s 'length')"
    if [ "$open_pr_count" != "0" ]; then
        echo "SKIP  $branch: still has an open pull request"
        skipped_count=$((skipped_count + 1))
        continue
    fi

    latest_closed_pr="$(
        list_same_repo_prs_for_branch closed "$branch" |
            jq -s 'sort_by(.closedAt // .mergedAt // "") | reverse | .[0] // empty'
    )"

    if [ -z "$latest_closed_pr" ]; then
        echo "SKIP  $branch: no closed pull request found"
        skipped_count=$((skipped_count + 1))
        continue
    fi

    pr_number="$(printf '%s' "$latest_closed_pr" | jq -r '.number')"
    pr_title="$(printf '%s' "$latest_closed_pr" | jq -r '.title')"
    merged_at="$(printf '%s' "$latest_closed_pr" | jq -r '.mergedAt // empty')"
    closed_at="$(printf '%s' "$latest_closed_pr" | jq -r '.closedAt // empty')"

    if [ -n "$merged_at" ]; then
        pr_state="merged"
        pr_time="$merged_at"
    else
        pr_state="closed"
        pr_time="$closed_at"
    fi

    if [ "$DELETE" = false ]; then
        echo "DRY   $branch: would delete after $pr_state PR #$pr_number ($pr_title) at $pr_time"
        continue
    fi

    encoded_branch="$(api_encode "$branch")"
    gh api \
        --method DELETE \
        "repos/$REPO/git/refs/heads/$encoded_branch" >/dev/null

    echo "DONE  $branch: deleted after $pr_state PR #$pr_number ($pr_title) at $pr_time"
    deleted_count=$((deleted_count + 1))
done

echo ""
echo "Summary: deleted=$deleted_count skipped=$skipped_count evaluated=${#BRANCHES[@]} mode=$([ "$DELETE" = true ] && echo delete || echo dry-run)"
