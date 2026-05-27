#!/usr/bin/env bash
set -euo pipefail

# review-codex-resume — resume the original resolve-issue Codex
# author with the reviewer artifacts handed in as the next turn.
# Prior session state lives at $HOME/.codex (bind-mounted from the
# host) so `codex exec resume --last` finds it.

if [ ! -d .githooks ]; then
  echo "::warning::review-codex-resume: .githooks/ not found (PR branched before hooks-in-CI landed); commits will skip pre-commit/pre-push validation"
fi

# shellcheck disable=SC1091
source .devcontainer/configure-codex.sh

if [ ! -f "$REVIEW_PROMPT_PATH" ]; then
  echo "::error::review-codex-resume: prompt file not found at $REVIEW_PROMPT_PATH"
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
    echo "::warning::review-codex-resume: caveman version mismatch — skipping rules"
    PROMPT_BODY=$(cat "$REVIEW_PROMPT_PATH")
  fi
else
  PROMPT_BODY=$(cat "$REVIEW_PROMPT_PATH")
fi

mkdir -p "$(dirname "$OUTPUT_PATH")"

# `codex exec resume --last` continues the most recent session in
# the cwd-filtered store. Per-(issue, run-id) bind ensures only the
# author's session is in scope.
timeout 30m codex exec resume \
  --last \
  --json \
  --dangerously-bypass-approvals-and-sandbox \
  "$PROMPT_BODY" \
  < /dev/null 2>&1 \
  | tee "$OUTPUT_LOG_PATH"

# Salvage block: if the prompt wrote a near-miss filename instead of
# $OUTPUT_PATH (e.g. fix-result.json because the schema is named
# fix-result-v1.json), rename it into place with a loud warning.
# Bounded allow-list — silently masking arbitrary filenames would
# hide real bugs. The warning is the canary; the real cure is to
# tighten the prompt so the agent uses $OUTPUT_PATH unambiguously.
if [ ! -s "$OUTPUT_PATH" ]; then
  OUTPUT_DIR=$(dirname "$OUTPUT_PATH")
  for candidate in "$OUTPUT_DIR/fix-result.json" "$OUTPUT_DIR/result.json"; do
    if [ -s "$candidate" ] && [ "$candidate" != "$OUTPUT_PATH" ]; then
      echo "::warning::review-codex-resume: prompt wrote $(basename "$candidate") instead of $(basename "$OUTPUT_PATH"); normalizing. Tighten the prompt to remove the ambiguity."
      mv "$candidate" "$OUTPUT_PATH"
      break
    fi
  done
fi

if [ ! -s "$OUTPUT_PATH" ]; then
  echo "::error::review-codex-resume: prompt did not produce $OUTPUT_PATH"
  exit 1
fi

echo "review-codex-resume: ${REVIEW_ROLE} produced $OUTPUT_PATH"
