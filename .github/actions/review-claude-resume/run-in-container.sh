#!/usr/bin/env bash
set -euo pipefail

# review-claude-resume — resume the original resolve-issue Claude
# author with reviewer artifacts handed in as the next turn. Prior
# session state lives at $HOME/.claude (bind-mounted from the host)
# so `claude --continue` finds it.

if [ ! -d .githooks ]; then
  echo "::warning::review-claude-resume: .githooks/ not found (PR branched before hooks-in-CI landed); commits will skip pre-commit/pre-push validation"
fi

if [ ! -f "$REVIEW_PROMPT_PATH" ]; then
  echo "::error::review-claude-resume: prompt file not found at $REVIEW_PROMPT_PATH"
  exit 1
fi

# Prepend caveman rules if available (optional — PRs branched
# before caveman merged will not have the file).
if [ -f .github/ci/caveman-rules.md ] && [ -f .github/ci/versions.env ]; then
  # shellcheck disable=SC1091
  source .github/ci/versions.env
  if grep -Fq "v${CAVEMAN_VERSION}" .github/ci/caveman-rules.md; then
    PROMPT_BODY=$(printf '%s\n\n%s' "$(cat .github/ci/caveman-rules.md)" "$(cat "$REVIEW_PROMPT_PATH")")
  else
    echo "::warning::review-claude-resume: caveman version mismatch; skipping rules"
    PROMPT_BODY=$(cat "$REVIEW_PROMPT_PATH")
  fi
else
  PROMPT_BODY=$(cat "$REVIEW_PROMPT_PATH")
fi

mkdir -p "$(dirname "$OUTPUT_PATH")"

# `claude --continue` resumes the most recent session in cwd. The
# bind-mounted /home/ci/.claude only contains the author's session
# for this (issue, run-id), so --continue is unambiguous.
timeout 30m claude -p \
  --continue \
  --dangerously-skip-permissions \
  --permission-mode=bypassPermissions \
  --model claude-sonnet-4-6 \
  --verbose \
  --output-format=stream-json \
  "$PROMPT_BODY" \
  < /dev/null 2>&1 \
  | tee "$OUTPUT_LOG_PATH"

if [ ! -s "$OUTPUT_PATH" ]; then
  echo "::error::review-claude-resume: prompt did not produce $OUTPUT_PATH"
  exit 1
fi

echo "review-claude-resume: ${REVIEW_ROLE} produced $OUTPUT_PATH"
