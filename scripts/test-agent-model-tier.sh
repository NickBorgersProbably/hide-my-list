#!/usr/bin/env bash
#
# test-agent-model-tier.sh — asserts every workflow step's bootstrap
# fallback copy of the Codex frontier/luna tier values matches the
# canonical values in .github/ci/agent-models.env.
#
# Background: workflows that check out `main` at the workspace root
# (script trust — the PR under review is checked out separately, see
# docs/agentic-pipeline-learnings.md §2.12) can't see a PR's own edits
# to .github/ci/agent-models.env until that PR merges. Every step that
# sources the file falls back to hard-coded defaults when the file is
# missing. This test is the guardrail that keeps those hard-coded
# copies from silently drifting from the canonical file — a future
# model swap that updates agent-models.env without updating every
# fallback copy fails here instead of quietly running a stale model on
# whichever workflow's fallback branch happens to fire next.
#
# Runs as a self-contained test: no network, no docker, no LLM.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.github/ci/agent-models.env"
WORKFLOWS_DIR="${REPO_ROOT}/.github/workflows"

failures=0
passes=0

ok() {
  passes=$((passes + 1))
  printf 'ok    %s\n' "$1"
}

fail() {
  failures=$((failures + 1))
  printf 'FAIL  %s\n' "$1" >&2
}

if [ ! -f "$ENV_FILE" ]; then
  fail "canonical file not found: ${ENV_FILE}"
  echo
  echo "${failures} failures"
  exit 1
fi

KEYS="CODEX_MODEL_FRONTIER CODEX_REASONING_FRONTIER CODEX_MODEL_LUNA CODEX_REASONING_LUNA"

# --- Parse canonical values from agent-models.env ---
declare -A canonical
for key in $KEYS; do
  value="$(grep -E "^${key}=" "$ENV_FILE" | head -1 | cut -d= -f2-)"
  if [ -z "$value" ]; then
    fail "${key} not found in ${ENV_FILE}"
    continue
  fi
  canonical["$key"]="$value"
  ok "canonical ${key}=${value}"
done

if [ "$failures" -gt 0 ]; then
  echo
  echo "${failures} failures"
  exit 1
fi

# --- Find every hard-coded fallback assignment under .github/workflows ---
# Matched by a literal `KEY=value` assignment at the start of a line
# (ignoring leading whitespace). Normal usage forwards CODEX_MODEL /
# CODEX_MODEL_REASONING_EFFORT via `${{ steps.model-tier.outputs.* }}`
# interpolation, which never matches this pattern — only a fallback
# `else` branch assigns these four keys directly as shell variables.
KEY_PATTERN="CODEX_MODEL_FRONTIER|CODEX_REASONING_FRONTIER|CODEX_MODEL_LUNA|CODEX_REASONING_LUNA"

matches_file="$(mktemp)"
trap 'rm -f "$matches_file"' EXIT

grep -rnE "^[[:space:]]*(${KEY_PATTERN})=" "$WORKFLOWS_DIR" > "$matches_file" || true

if [ ! -s "$matches_file" ]; then
  fail "no fallback KEY=VALUE assignments found under ${WORKFLOWS_DIR} — did the bootstrap fallback move or get removed?"
  echo
  echo "${failures} failures"
  exit 1
fi

found_keys=""
while IFS= read -r line; do
  # line is "<file>:<lineno>:<content>"
  file="${line%%:*}"
  rest="${line#*:}"
  content="${rest#*:}"
  key="$(printf '%s\n' "$content" | sed -E 's/^[[:space:]]*([A-Z_]+)=.*$/\1/')"
  value="$(printf '%s\n' "$content" | sed -E 's/^[[:space:]]*[A-Z_]+=([^[:space:]]*)[[:space:]]*$/\1/')"
  rel_file="${file#"${REPO_ROOT}"/}"
  expected="${canonical[$key]:-}"
  if [ -z "$expected" ]; then
    fail "${rel_file}: unrecognized key in fallback assignment: ${content}"
    continue
  fi
  if [ "$value" = "$expected" ]; then
    ok "${rel_file}: ${key}=${value} matches agent-models.env"
    found_keys="${found_keys} ${key}"
  else
    fail "${rel_file}: ${key}=${value} does not match agent-models.env's ${expected}"
  fi
done < "$matches_file"

# Every canonical key should appear in at least one fallback block —
# otherwise a step that sources the file with no fallback would go
# uncaught by this test.
for key in $KEYS; do
  case " $found_keys " in
    *" $key "*) ;;
    *) fail "no fallback assignment found anywhere for ${key} (every step that sources agent-models.env should fall back to it)" ;;
  esac
done

echo
total=$((passes + failures))
printf '%s passes, %s failures (out of %s assertions)\n' "$passes" "$failures" "$total"
[ "$failures" -eq 0 ]
