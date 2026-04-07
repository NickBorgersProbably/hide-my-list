#!/usr/bin/env bash
# get-latest-merge-decision-comment.sh — fetch the most recent trusted merge decision comment for a PR

set -euo pipefail

: "${GH_TOKEN:?GH_TOKEN is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${PR_NUMBER:?PR_NUMBER is required}"

MAX_ATTEMPTS="${MERGE_DECISION_COMMENT_MAX_ATTEMPTS:-5}"
RETRY_DELAY_SECONDS="${MERGE_DECISION_COMMENT_RETRY_DELAY_SECONDS:-3}"
MERGE_DECISION_MARKER='<!-- codex-merge-decision -->'

if ! [[ "$MAX_ATTEMPTS" =~ ^[1-9][0-9]*$ ]]; then
    echo "MAX_ATTEMPTS must be a positive integer, got: ${MAX_ATTEMPTS}" >&2
    exit 1
fi

if ! [[ "$RETRY_DELAY_SECONDS" =~ ^[0-9]+$ ]]; then
    echo "RETRY_DELAY_SECONDS must be a non-negative integer, got: ${RETRY_DELAY_SECONDS}" >&2
    exit 1
fi

for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
    comment_json="$(
        gh api "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments" \
            --paginate \
            | jq -s 'add
                | map(
                    select(
                      .user.login == "github-actions[bot]"
                      and ((.body // "") | contains("'"${MERGE_DECISION_MARKER}"'"))
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
