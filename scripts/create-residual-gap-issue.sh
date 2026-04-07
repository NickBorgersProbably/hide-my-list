#!/usr/bin/env bash
set -euo pipefail

: "${GH_TOKEN:?GH_TOKEN is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${PR_NUMBER:?PR_NUMBER is required}"

LINKED_ISSUE="${LINKED_ISSUE:-}"
RESIDUAL_BLOCK_START="<codex-residual-gap>"
RESIDUAL_BLOCK_END="</codex-residual-gap>"
DEDUP_MARKER="<!-- codex-residual-gap:pr-${PR_NUMBER} -->"

comment_json="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments" \
  --paginate \
  --jq '[.[] | select(.body | contains("## Merge Decision"))] | last')"

if [[ -z "${comment_json}" || "${comment_json}" == "null" ]]; then
  echo "No merge decision comment found for PR #${PR_NUMBER}"
  exit 0
fi

comment_body="$(jq -r '.body // ""' <<<"${comment_json}")"
comment_body="${comment_body//$'\r'/}"

decision_line="$(grep -m1 -E '^\*\*Decision:' <<<"${comment_body}" || true)"
case "${decision_line}" in
  *GO-WITH-RESERVATIONS*)
    ;;
  *)
    echo "Latest merge decision for PR #${PR_NUMBER} is not GO-WITH-RESERVATIONS"
    exit 0
    ;;
esac

residual_block="$(awk -v start="${RESIDUAL_BLOCK_START}" -v end="${RESIDUAL_BLOCK_END}" '
  $0 == start { in_block = 1; next }
  $0 == end { in_block = 0; exit }
  in_block { print }
' <<<"${comment_body}")"

if [[ -z "${residual_block//[$' \t\r\n']}" ]]; then
  echo "No residual gap block found for PR #${PR_NUMBER}"
  exit 0
fi

residual_title="$(sed -n 's#^<title>\(.*\)</title>$#\1#p' <<<"${residual_block}" | head -n1)"
residual_body="$(sed -n '/<body>/,/<\/body>/p' <<<"${residual_block}" | sed '1s#.*<body>##; $s#</body>.*##')"

if [[ -z "${residual_title//[$' \t\r\n']}" ]]; then
  echo "Residual gap block omitted a title; skipping follow-up issue creation"
  exit 0
fi

if [[ -z "${residual_body//[$' \t\r\n']}" ]]; then
  echo "Residual gap block omitted a body; skipping follow-up issue creation"
  exit 0
fi

if grep -qiE '^[[:space:]]*<.+>[[:space:]]*$' <<<"${residual_title}"; then
  echo "Residual gap title still looks like a placeholder; skipping follow-up issue creation"
  exit 0
fi

if grep -qiE '^[[:space:]]*<.+>[[:space:]]*$' <<<"$(head -n1 <<<"${residual_body}")"; then
  echo "Residual gap body still looks like a placeholder; skipping follow-up issue creation"
  exit 0
fi

find_existing_issue() {
  local page=1
  local issues_json existing_issue issue_count

  while :; do
    issues_json="$(gh api "repos/${GITHUB_REPOSITORY}/issues?state=all&per_page=100&page=${page}")"
    existing_issue="$(
      jq -r --arg marker "${DEDUP_MARKER}" '
        .[] |
        select(.pull_request | not) |
        select((.body // "") | contains($marker)) |
        .number
      ' <<<"${issues_json}" | head -n1
    )"

    if [[ -n "${existing_issue}" ]]; then
      printf '%s\n' "${existing_issue}"
      return 0
    fi

    issue_count="$(jq 'length' <<<"${issues_json}")"
    if (( issue_count < 100 )); then
      break
    fi

    page=$((page + 1))
  done

  return 1
}

if existing_issue_number="$(find_existing_issue)"; then
  echo "Residual gap issue already exists: #${existing_issue_number}"
  exit 0
fi

residual_body_full="${residual_body}"
residual_body_full+=$'\n\n---\n'
residual_body_full+="Created automatically from the merge decision on merged PR #${PR_NUMBER}."
if [[ -n "${LINKED_ISSUE}" ]]; then
  residual_body_full+=$'\n'"Original linked issue: #${LINKED_ISSUE}."
fi
residual_body_full+=$'\n'"${DEDUP_MARKER}"

new_issue_url="$(gh issue create \
  --repo "${GITHUB_REPOSITORY}" \
  --title "${residual_title}" \
  --body "${residual_body_full}")"
new_issue_number="$(basename "${new_issue_url}")"

echo "Created residual gap issue: ${new_issue_url}"

if [[ -n "${LINKED_ISSUE}" ]]; then
  gh issue comment "${LINKED_ISSUE}" \
    --repo "${GITHUB_REPOSITORY}" \
    --body "Merged PR #${PR_NUMBER} resolved part of this issue under GO-WITH-RESERVATIONS. Residual follow-up issue: #${new_issue_number}" \
    >/dev/null
fi
