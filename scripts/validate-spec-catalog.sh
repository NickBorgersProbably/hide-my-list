#!/usr/bin/env bash
# Validate that every spec file registered in the review-classify action's
# is_spec_md() list is also registered in docs/index.md (public TOC) and
# DEV-AGENTS.md Key Files (contributor catalog).
#
# Background: when a spec file is split (e.g. HEARTBEAT.md -> docs/heartbeat-checks.md)
# the new file often gets added to some catalogs but not others, leading to
# reviewers flagging "missing from docs/index.md / Key Files" on every PR
# that touches the spec surface. This check catches that drift repo-wide
# instead of per-PR.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

CLASSIFIER=".github/actions/review-classify/action.yml"
INDEX="docs/index.md"
CATALOG="DEV-AGENTS.md"

for f in "$CLASSIFIER" "$INDEX" "$CATALOG"; do
  if [[ ! -f "$f" ]]; then
    echo "ERROR: required file missing: $f" >&2
    exit 1
  fi
done

# Extract docs/*.md spec files from the classifier's is_spec_md() case block.
mapfile -t spec_docs < <(
  awk '/^[[:space:]]*is_spec_md\(\)/{in_fn=1; next} in_fn && /^[[:space:]]*\}/{in_fn=0} in_fn' "$CLASSIFIER" \
    | grep -oE 'docs/[A-Za-z0-9_./-]+\.md' \
    | sort -u
)

if (( ${#spec_docs[@]} == 0 )); then
  echo "ERROR: could not extract any docs/*.md entries from $CLASSIFIER is_spec_md()" >&2
  exit 1
fi

missing=()
for doc in "${spec_docs[@]}"; do
  if [[ ! -f "$doc" ]]; then
    missing+=("$doc: referenced in $CLASSIFIER but file does not exist")
    continue
  fi
  base="$(basename "$doc")"
  if ! grep -qF "$base" "$INDEX"; then
    missing+=("$doc: not linked from $INDEX")
  fi
  if ! grep -qF "$doc" "$CATALOG"; then
    missing+=("$doc: not listed in $CATALOG Key Files")
  fi
done

if (( ${#missing[@]} > 0 )); then
  echo "ERROR: spec-catalog drift between $CLASSIFIER, $INDEX, $CATALOG:" >&2
  for m in "${missing[@]}"; do
    echo "  - $m" >&2
  done
  echo "" >&2
  echo "Fix: add the missing file to all three catalogs, or remove it from $CLASSIFIER if no longer a spec." >&2
  exit 1
fi

echo "Spec catalogs consistent: ${#spec_docs[@]} docs/*.md spec files registered in all three catalogs"
