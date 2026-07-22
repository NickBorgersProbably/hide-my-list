#!/usr/bin/env bash
#
# issue-pr-claims.sh — Determine whether a GitHub issue already has work in flight.
#
# The Issue Resolution Agent (.github/workflows/codex.yml) dispatches on
# `issues: [opened, reopened]` with no approval gate. Two independent authors —
# the pipeline and a human or local agent working in a clone — can therefore
# start on the same issue and each open a PR, and each of those PRs runs the
# full multi-agent review pipeline.
#
# This helper answers two questions deterministically:
#
#   is-claimed <issue>   Should dispatch be suppressed? (pre-flight)
#   duplicates <issue>   Which open PRs target this issue? (post-run)
#
# Matching is done with an explicit closing-keyword regex over PR bodies rather
# than GitHub's full-text search. Full-text search matches a bare `#N` anywhere
# in the body, so a PR that merely cites an issue as context ("root cause is
# #629") reads as a claim on it.
#
# Testability: every GitHub call goes through "${GH_BIN:-gh}", so
# scripts/test-issue-pr-claims.sh can substitute a mock.

set -euo pipefail

GH="${GH_BIN:-gh}"
MERGED_SCAN_LIMIT="${MERGED_SCAN_LIMIT:-50}"
OPEN_SCAN_LIMIT="${OPEN_SCAN_LIMIT:-100}"

usage() {
  cat <<'EOF'
Usage:
  issue-pr-claims.sh is-claimed <issue-number> [--repo <owner/name>]
      Exit 0 and print a reason when the issue is already claimed (dispatch
      should be suppressed). Exit 1 when no claim exists.

  issue-pr-claims.sh duplicates <issue-number> [--repo <owner/name>]
      Print every open PR number whose body closes the issue, one per line.
      Exit 0 regardless of count; callers decide what a duplicate means.

  issue-pr-claims.sh issue-state <issue-number> [--repo <owner/name>]
      Print the issue state (OPEN / CLOSED).

Environment:
  GH_BIN              gh executable to use (default: gh)
  OPEN_SCAN_LIMIT     open PRs to scan (default: 100)
  MERGED_SCAN_LIMIT   merged PRs to scan (default: 50)
EOF
}

# GitHub's closing keywords, anchored so that a bare `#N` reference does not match.
# Matches: "Closes #12", "Fixed: #12", "resolve #12". Does not match: "see #12".
closing_ref_filter() {
  cat <<'EOF'
def closes($n):
  .body != null
  and (.body | test("(?i)(^|[^a-z0-9_])(close[sd]?|fix(e[sd])?|resolve[sd]?)\\s*:?\\s*#" + $n + "([^0-9]|$)"));
[.[] | select(closes($num)) | .number] | .[]
EOF
}

require_issue_number() {
  case "${1:-}" in
    ''|*[!0-9]*)
      echo "error: issue number must be a positive integer, got: '${1:-}'" >&2
      exit 2
      ;;
  esac
}

# Collect --repo into REPO_ARGS so it is forwarded to every gh call.
REPO_ARGS=()
parse_repo_flag() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --repo)
        if [ -z "${2:-}" ]; then
          echo "error: --repo requires a value" >&2
          exit 2
        fi
        REPO_ARGS=(--repo "$2")
        shift 2
        ;;
      *)
        echo "error: unexpected argument: $1" >&2
        exit 2
        ;;
    esac
  done
}

prs_closing_issue() {
  local state="$1" limit="$2" issue="$3"
  "$GH" pr list --state "$state" --limit "$limit" --json number,body "${REPO_ARGS[@]}" \
    | jq -r --arg num "$issue" "$(closing_ref_filter)"
}

issue_state() {
  "$GH" issue view "$1" --json state "${REPO_ARGS[@]}" --jq '.state'
}

cmd="${1:-}"
[ "$#" -gt 0 ] && shift

case "$cmd" in
  is-claimed)
    issue="${1:-}"
    require_issue_number "$issue"
    shift
    parse_repo_flag "$@"

    # A closed issue means something already resolved it. The workflow triggers
    # on `reopened`, so an intentional re-dispatch arrives with state OPEN.
    state="$(issue_state "$issue")"
    if [ "$state" = "CLOSED" ]; then
      echo "issue #${issue} is CLOSED; nothing to dispatch"
      exit 0
    fi

    open_prs="$(prs_closing_issue open "$OPEN_SCAN_LIMIT" "$issue")"
    if [ -n "$open_prs" ]; then
      echo "open PR(s) already target issue #${issue}: $(echo "$open_prs" | tr '\n' ' ' | sed 's/ $//')"
      exit 0
    fi

    merged_prs="$(prs_closing_issue merged "$MERGED_SCAN_LIMIT" "$issue")"
    if [ -n "$merged_prs" ]; then
      echo "merged PR(s) already resolved issue #${issue}: $(echo "$merged_prs" | tr '\n' ' ' | sed 's/ $//')"
      exit 0
    fi

    echo "no PR targets issue #${issue}"
    exit 1
    ;;

  duplicates)
    issue="${1:-}"
    require_issue_number "$issue"
    shift
    parse_repo_flag "$@"
    prs_closing_issue open "$OPEN_SCAN_LIMIT" "$issue"
    ;;

  issue-state)
    issue="${1:-}"
    require_issue_number "$issue"
    shift
    parse_repo_flag "$@"
    issue_state "$issue"
    ;;

  ''|-h|--help|help)
    usage
    exit 0
    ;;

  *)
    echo "error: unknown subcommand: $cmd" >&2
    usage >&2
    exit 2
    ;;
esac
