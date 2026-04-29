#!/usr/bin/env bash
#
# cleanup-ci-sessions.sh — prune persisted resolve-issue author
# sessions from the self-hosted runner host once they're no longer
# needed for review cycles.
#
# Layout (see scripts/ci-session-store.sh):
#   /srv/ci-sessions/<agent>/<issue>/<run-id>/
#
# Pruning policy (defensive — keeps any session that might still be
# resumed by an active review cycle):
#   - Skip if the issue's PR is open. Open PRs may still iterate.
#   - Otherwise, delete subdirs older than $MAX_AGE_DAYS.
#
# Usage:
#   ./scripts/cleanup-ci-sessions.sh                # dry-run
#   ./scripts/cleanup-ci-sessions.sh --apply        # actually delete
#   MAX_AGE_DAYS=14 ./scripts/cleanup-ci-sessions.sh --apply
#
# Run on the runner host (where the bind-mounted dirs live), not in a
# container. Requires `gh` authenticated against the repo. Uses `sudo
# rm -rf` because the dirs are owned by the container's `ci` UID
# (1000), which differs from the runner UID.

set -euo pipefail

ROOT="${CI_SESSION_ROOT:-/srv/ci-sessions}"
MAX_AGE_DAYS="${MAX_AGE_DAYS:-7}"
APPLY="false"

if [ "${1:-}" = "--apply" ]; then
  APPLY="true"
fi

if [ ! -d "$ROOT" ]; then
  echo "cleanup-ci-sessions: nothing to do; ${ROOT} does not exist"
  exit 0
fi

REPO="${REPO:-$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)}"
if [ -z "$REPO" ]; then
  echo "cleanup-ci-sessions: REPO not set and gh repo view failed; aborting" >&2
  exit 1
fi

skipped=0
deleted=0
preserved=0

# find directories at depth 3: <root>/<agent>/<issue>/<run-id>/
while IFS= read -r run_dir; do
  issue=$(basename "$(dirname "$run_dir")")
  agent=$(basename "$(dirname "$(dirname "$run_dir")")")

  if [ -z "$issue" ] || [ -z "$agent" ]; then
    continue
  fi

  # Determine PR state for the issue. Search by closing keywords;
  # match the same heuristic resolve-issue uses to find duplicate PRs.
  pr_state=$(gh pr list \
    --repo "$REPO" \
    --state all \
    --search "Resolves #${issue} OR Fixes #${issue} OR Closes #${issue}" \
    --json state \
    --jq '.[0].state // "NONE"' 2>/dev/null || echo "NONE")

  if [ "$pr_state" = "OPEN" ]; then
    skipped=$((skipped + 1))
    echo "skip  ${agent}/${issue}/$(basename "$run_dir") (PR still open)"
    continue
  fi

  age_minutes=$(( ($(date +%s) - $(stat -c %Y "$run_dir")) / 60 ))
  age_days=$(( age_minutes / 60 / 24 ))

  if [ "$age_days" -lt "$MAX_AGE_DAYS" ]; then
    preserved=$((preserved + 1))
    echo "keep  ${agent}/${issue}/$(basename "$run_dir") (${age_days}d < ${MAX_AGE_DAYS}d)"
    continue
  fi

  if [ "$APPLY" = "true" ]; then
    sudo rm -rf "$run_dir"
    deleted=$((deleted + 1))
    echo "rm    ${agent}/${issue}/$(basename "$run_dir") (${age_days}d, PR=${pr_state})"
  else
    deleted=$((deleted + 1))
    echo "would-rm  ${agent}/${issue}/$(basename "$run_dir") (${age_days}d, PR=${pr_state})"
  fi
done < <(find "$ROOT" -mindepth 3 -maxdepth 3 -type d 2>/dev/null)

if [ "$APPLY" = "true" ]; then
  echo "cleanup-ci-sessions: deleted=${deleted} preserved=${preserved} skipped=${skipped}"
else
  echo "cleanup-ci-sessions (dry run): would-delete=${deleted} preserved=${preserved} skipped=${skipped}"
  echo "Re-run with --apply to actually delete."
fi
