#!/usr/bin/env bash
set -euo pipefail

# shellcheck disable=SC1091
source .devcontainer/configure-codex.sh

if [ ! -f "$REVIEW_PROMPT_PATH" ]; then
  echo "::error::review-codex-run: prompt file not found at $REVIEW_PROMPT_PATH"
  exit 1
fi

PROMPT_BODY=$(cat "$REVIEW_PROMPT_PATH")

mkdir -p "$(dirname "$OUTPUT_PATH")"

# The prompt body instructs Codex to write its structured
# result JSON to $OUTPUT_PATH and conform to the schema in
# .github/scripts/review/schema/.
timeout 30m codex exec \
  --json \
  --dangerously-bypass-approvals-and-sandbox \
  "$PROMPT_BODY" \
  < /dev/null 2>&1 \
  | tee "$OUTPUT_LOG_PATH"

if [ ! -s "$OUTPUT_PATH" ]; then
  echo "::error::review-codex-run: prompt did not produce $OUTPUT_PATH"
  exit 1
fi

echo "review-codex-run: ${REVIEW_ROLE} produced $OUTPUT_PATH"
