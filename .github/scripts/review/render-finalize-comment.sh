#!/usr/bin/env bash
# Renders and posts the per-cycle merge-decision PR comment.
#
# Branches on $CATEGORY (go | reviewer_blockers | pipeline_error |
# cycle_capped) to choose template + per-role status table content.
#
# Inputs (env):
#   GH_TOKEN, REPO, PR_NUMBER  — gh auth + target
#   POST_FIX_SHA, REVIEWED_SHA, CYCLE, VERDICT, CATEGORY
#   GITHUB_SERVER_URL, GITHUB_REPOSITORY, GITHUB_RUN_ID  — workflow run link
#   VERDICT_DIR     — runner.temp/verdict (verdict-<sha>.json may be absent)
#   REVIEWERS_DIR   — runner.temp/reviewers (per-role subdirs, may be absent)
#
# Output: posts ONE comment via `gh pr comment`. No stdout consumed by
# the workflow.

set -euo pipefail

VERDICT_PATH="${VERDICT_DIR}/verdict-${POST_FIX_SHA}.json"
RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"

# Human-readable category labels and emoji.
case "$CATEGORY" in
  go)
    HEADER_EMOJI="🟢"
    HEADER_VERDICT="**GO** — ready to merge"
    ;;
  reviewer_blockers)
    HEADER_EMOJI="🔴"
    HEADER_VERDICT="**NO-GO** — reviewer-flagged blockers"
    ;;
  pipeline_error)
    HEADER_EMOJI="⚠️"
    HEADER_VERDICT="**NO-GO (pipeline error)** — not a code-review issue"
    ;;
  cycle_capped)
    HEADER_EMOJI="🛑"
    HEADER_VERDICT="**NO-GO (cycle cap)** — fixer didn't converge"
    ;;
  inherited)
    if [ "$VERDICT" = "GO" ]; then
      HEADER_EMOJI="🟢"
      HEADER_VERDICT="**GO (inherited)** — verdict re-stamped from prior cycle"
    else
      HEADER_EMOJI="🔴"
      HEADER_VERDICT="**NO-GO (inherited)** — verdict re-stamped from prior cycle"
    fi
    ;;
  *)
    HEADER_EMOJI="⚠️"
    HEADER_VERDICT="**NO-GO** — unknown category \`${CATEGORY}\`"
    ;;
esac

# Per-role table row. $1=role, emits one tab-separated line.
role_row() {
  local role="$1"
  local emoji
  case "$role" in
    design)   emoji="🎨" ;;
    security) emoji="🔐" ;;
    docs)     emoji="📚" ;;
    prompt)   emoji="✍️" ;;
    psych)    emoji="🧠" ;;
    *)        emoji="🤖" ;;
  esac

  local dir="${REVIEWERS_DIR}/reviewer-${role}-${REVIEWED_SHA}"
  if [ ! -d "$dir" ]; then
    printf "| %s %s | _did not run_ | — |\n" "$emoji" "$role"
    return
  fi

  local artifact="${dir}/${role}-result.json"
  if [ ! -f "$artifact" ]; then
    printf "| %s %s | _no artifact_ | — |\n" "$emoji" "$role"
    return
  fi

  local decision blocker_count notes_chunk url status_chunk artifact_sha sha_warn
  decision=$(jq -r '.decision // "?"' "$artifact")
  blocker_count=$(jq -r '.blocking_issues | length' "$artifact")
  artifact_sha=$(jq -r '.reviewed_sha // ""' "$artifact")

  # When the artifact's reviewed_sha disagrees with REVIEWED_SHA, the
  # reviewer is the source of a pipeline_error mixed-epoch failure.
  # Surface it directly in the row so the operator doesn't have to
  # cross-reference aggregator output with reviewer JSON.
  sha_warn=""
  if [ -n "$artifact_sha" ] && [ -n "$REVIEWED_SHA" ] && [ "$artifact_sha" != "$REVIEWED_SHA" ]; then
    sha_warn=" ⚠️ wrong sha (\`${artifact_sha:0:7}\`)"
  fi

  case "$decision" in
    approve)         status_chunk="✅ approve" ;;
    request_changes) status_chunk="🔴 changes" ;;
    comment)         status_chunk="🟡 comment" ;;
    abstain)         status_chunk="⚪ abstain" ;;
    *)               status_chunk="❓ $decision" ;;
  esac
  status_chunk="${status_chunk}${sha_warn}"

  url=""
  if [ -f "${dir}/${role}-comment-url.txt" ]; then
    url=$(tr -d '[:space:]' < "${dir}/${role}-comment-url.txt")
  fi

  if [ "$blocker_count" -gt 0 ]; then
    if [ -n "$url" ]; then
      notes_chunk="${blocker_count} blocker(s) — [view comment](${url})"
    else
      notes_chunk="${blocker_count} blocker(s)"
    fi
  elif [ -n "$url" ]; then
    notes_chunk="[view comment](${url})"
  else
    notes_chunk="—"
  fi

  printf "| %s %s | %s | %s |\n" "$emoji" "$role" "$status_chunk" "$notes_chunk"
}

# Render the per-role table for cycles where reviewers ran.
render_table() {
  echo "| Agent | Verdict | Notes |"
  echo "|-------|---------|-------|"
  for role in design security docs prompt psych; do
    role_row "$role"
  done
}

# Render unaddressed-blocker bullet list, hyperlinking each blocker
# back to the reviewer's PR comment when we have its URL.
render_blockers() {
  if [ ! -f "$VERDICT_PATH" ]; then
    return
  fi
  local count
  count=$(jq -r '.unaddressed_blocker_ids | length' "$VERDICT_PATH")
  if [ "$count" -eq 0 ]; then
    return
  fi

  echo "### Unaddressed blockers"
  jq -r '.unaddressed_blocker_ids[]' "$VERDICT_PATH" | while IFS=/ read -r role rest; do
    full="${role}/${rest}"
    url_file="${REVIEWERS_DIR}/reviewer-${role}-${REVIEWED_SHA}/${role}-comment-url.txt"
    if [ -f "$url_file" ]; then
      url=$(tr -d '[:space:]' < "$url_file")
      printf -- "- [\`%s\`](%s)\n" "$full" "$url"
    else
      printf -- "- \`%s\`\n" "$full"
    fi
  done
  echo
}

# Reasons block — keep aggregator-internal vocabulary out of the
# operator-facing text by prefixing with a plain-English lead.
render_reasons() {
  if [ ! -f "$VERDICT_PATH" ]; then
    return
  fi
  local has
  has=$(jq -r '.reasons | length' "$VERDICT_PATH")
  if [ "$has" -eq 0 ]; then
    return
  fi
  echo "<details><summary>Aggregator detail</summary>"
  echo
  jq -r '.reasons[]' "$VERDICT_PATH" | sed 's/^/- /'
  echo
  echo "</details>"
  echo
}

# Cycle history fetched from review/cycle commit statuses on prior
# SHAs. Only used by the cycle_capped path.
render_cycle_history() {
  echo "**Cycle history**"
  echo
  # Walk the first-parent chain back from POST_FIX_SHA, looking for
  # review/cycle statuses. Bound depth so we don't hammer the API on
  # long-lived branches.
  local current="$POST_FIX_SHA"
  local depth=0
  local seen_cycles=""
  while [ $depth -lt 20 ]; do
    local cycle_desc
    cycle_desc=$(gh api "repos/${REPO}/commits/${current}/statuses" \
      --jq "[.[] | select(.context == \"review/cycle\")] | sort_by(.created_at) | last | .description // empty" \
      2>/dev/null || echo "")
    if [ -n "$cycle_desc" ]; then
      if ! echo "$seen_cycles" | grep -qx "$cycle_desc"; then
        printf -- "- Cycle %s — \`%s\`\n" "$cycle_desc" "${current:0:7}"
        seen_cycles="${seen_cycles}${cycle_desc}\n"
      fi
    fi
    local parent
    parent=$(gh api "repos/${REPO}/commits/${current}" --jq '.parents[0].sha // empty' 2>/dev/null || echo "")
    if [ -z "$parent" ]; then
      break
    fi
    current="$parent"
    depth=$((depth + 1))
  done
  echo
}

# Plain-English next-step block — what the operator should actually do.
render_next_step() {
  case "$CATEGORY" in
    go|inherited)
      ;;
    reviewer_blockers)
      echo "**Next step:** address the blockers above and push a new commit."
      echo
      ;;
    pipeline_error)
      echo "**Next step:** this is a pipeline bug, not a code-review concern."
      echo "Retry with a \`/review\` comment, or if the same error recurs,"
      echo "file an issue against the failing reviewer/orchestrator."
      echo
      ;;
    cycle_capped)
      echo "**Next step:** the autofix loop didn't converge in MAX cycles."
      echo "A human needs to either (a) admin-merge if the diff is acceptable"
      echo "as-is, or (b) force-push a rebase to reset the cycle counter and"
      echo "let the pipeline run fresh."
      echo
      ;;
  esac
}

BODY_FILE=$(mktemp)
{
  # Markers for downstream automation:
  #   codex-merge-decision — historical, pre-v2; preserved for any
  #                          existing scrapers.
  #   review-pipeline-v2:<verdict>:<category> — current, machine-readable.
  #   review-sha:<sha> — stale-comment marker. A future render that sees
  #                      this comment is from a different SHA can be
  #                      detected without parsing the body.
  echo "<!-- codex-merge-decision -->"
  if [ "$VERDICT" = "GO" ]; then
    echo "<!-- review-pipeline-v2:go:${CATEGORY} -->"
  else
    echo "<!-- review-pipeline-v2:no-go:${CATEGORY} -->"
  fi
  echo "<!-- review-sha:${POST_FIX_SHA} -->"
  echo

  echo "## 📋 Agent Review Summary  ·  cycle ${CYCLE}  ·  \`${POST_FIX_SHA:0:7}\`"
  echo
  echo "**Verdict:** ${HEADER_EMOJI} ${HEADER_VERDICT}"
  echo "**Workflow Run:** [#${GITHUB_RUN_ID}](${RUN_URL})"
  if [ -n "$REVIEWED_SHA" ] && [ "$REVIEWED_SHA" != "$POST_FIX_SHA" ]; then
    echo "**Reviewed SHA:** \`${REVIEWED_SHA:0:7}\` (fixer pushed → \`${POST_FIX_SHA:0:7}\`)"
  fi
  echo

  case "$CATEGORY" in
    cycle_capped)
      render_cycle_history
      ;;
    inherited)
      # Inherited path has neither verdict.json nor reviewer artifacts
      # for the current SHA. Surface the synthesized reason from the
      # caller (review-pipeline.yml's inherit job) instead of an
      # empty table.
      if [ -n "${SYNTH_REASON:-}" ]; then
        echo "${SYNTH_REASON}"
        echo
      fi
      ;;
    *)
      render_table
      echo
      render_blockers
      render_reasons
      ;;
  esac

  render_next_step

  echo "<sub>review-pipeline v2  ·  cycle ${CYCLE}</sub>"
} > "$BODY_FILE"

gh pr comment "$PR_NUMBER" --repo "$REPO" --body-file "$BODY_FILE"
rm -f "$BODY_FILE"
