#!/usr/bin/env bash
set -euo pipefail

if [ ! -f "$REVIEW_PROMPT_PATH" ]; then
  echo "::error::review-claude-run: prompt file not found at $REVIEW_PROMPT_PATH"
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
    echo "::warning::review-claude-run: caveman version mismatch; skipping rules"
    PROMPT_BODY=$(cat "$REVIEW_PROMPT_PATH")
  fi
else
  PROMPT_BODY=$(cat "$REVIEW_PROMPT_PATH")
fi

mkdir -p "$(dirname "$OUTPUT_PATH")"

# The prompt body instructs Claude to write its structured
# result JSON to $OUTPUT_PATH. Keep stdout as stream-json so
# the uploaded log stays machine-readable like the Codex path.
timeout 30m claude -p \
  --dangerously-skip-permissions \
  --permission-mode=bypassPermissions \
  --model claude-sonnet-4-6 \
  --output-format=stream-json \
  "$PROMPT_BODY" \
  < /dev/null 2>&1 \
  | tee "$OUTPUT_LOG_PATH"

if [ ! -s "$OUTPUT_PATH" ]; then
  echo "::error::review-claude-run: prompt did not produce $OUTPUT_PATH"
  exit 1
fi

echo "review-claude-run: ${REVIEW_ROLE} produced $OUTPUT_PATH"
