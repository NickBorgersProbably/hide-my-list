#!/usr/bin/env bash
# get-latest-merge-decision-comment.sh — fetch the most recent trusted merge decision comment for a PR

set -euo pipefail

: "${GH_TOKEN:?GH_TOKEN is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${PR_NUMBER:?PR_NUMBER is required}"

MAX_ATTEMPTS="${MERGE_DECISION_COMMENT_MAX_ATTEMPTS:-5}"
RETRY_DELAY_SECONDS="${MERGE_DECISION_COMMENT_RETRY_DELAY_SECONDS:-3}"
MIN_COMMENT_ID="${MERGE_DECISION_COMMENT_MIN_ID:-0}"
MERGE_DECISION_MARKER='<!-- codex-merge-decision -->'

if ! [[ "$MAX_ATTEMPTS" =~ ^[1-9][0-9]*$ ]]; then
    echo "MAX_ATTEMPTS must be a positive integer, got: ${MAX_ATTEMPTS}" >&2
    exit 1
fi

if ! [[ "$RETRY_DELAY_SECONDS" =~ ^[0-9]+$ ]]; then
    echo "RETRY_DELAY_SECONDS must be a non-negative integer, got: ${RETRY_DELAY_SECONDS}" >&2
    exit 1
fi

if ! [[ "$MIN_COMMENT_ID" =~ ^[0-9]+$ ]]; then
    echo "MIN_COMMENT_ID must be a non-negative integer, got: ${MIN_COMMENT_ID}" >&2
    exit 1
fi

for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
    comment_json="$(
        gh api "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments" \
            --paginate \
            | jq -s \
                --arg merge_decision_marker "${MERGE_DECISION_MARKER}" \
                --argjson min_comment_id "${MIN_COMMENT_ID}" '
                add
                | map(
                    select(
                      .user.login == "github-actions[bot]"
                      and ((.id // 0) > $min_comment_id)
                      and ((.body // "") | contains($merge_decision_marker))
                      and ((.body // "") | contains("## Merge Decision"))
                    )
                  )
                | sort_by(.id)
                | last'
    )"

    if [[ -n "${comment_json}" && "${comment_json}" != "null" ]]; then
        printf '%s\n' "${comment_json}"
        exit 0
    fi

    if (( attempt < MAX_ATTEMPTS )); then
        echo "Merge decision comment not yet visible for PR #${PR_NUMBER} (attempt ${attempt}/${MAX_ATTEMPTS}); sleeping ${RETRY_DELAY_SECONDS}s" >&2
        sleep "${RETRY_DELAY_SECONDS}"
    fi
done

echo "Merge decision comment not found for PR #${PR_NUMBER} after ${MAX_ATTEMPTS} attempts" >&2
exit 1
