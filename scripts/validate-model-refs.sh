#!/usr/bin/env bash
# Validate that every `litellm/<id>` model reference in canonical spec files
# (HEARTBEAT.md, setup/cron/*.md, docs/architecture.md, setup/README.md)
# has a matching entry in setup/openclaw.json.template's models array.
#
# Background: model ids drift across spec files on every rename. This check
# catches drift before PR review does.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

TEMPLATE="setup/openclaw.json.template"
SPEC_FILES=(
  "HEARTBEAT.md"
  "docs/architecture.md"
  "docs/openclaw-integration.md"
  "setup/README.md"
)
for f in setup/cron/*.md; do
  SPEC_FILES+=("$f")
done

if [[ ! -f "$TEMPLATE" ]]; then
  echo "ERROR: $TEMPLATE not found" >&2
  exit 1
fi

template_ids="$(grep -oE '"id":[[:space:]]*"[^"]+"' "$TEMPLATE" | sed -E 's/.*"([^"]+)"$/\1/' | sort -u)"

missing=()
for spec in "${SPEC_FILES[@]}"; do
  [[ -f "$spec" ]] || continue
  while IFS= read -r ref; do
    id="${ref#litellm/}"
    if ! grep -qx "$id" <<<"$template_ids"; then
      missing+=("$spec: litellm/$id (not in $TEMPLATE models[].id)")
    fi
  done < <(grep -oE 'litellm/[A-Za-z0-9._-]+' "$spec" | sort -u)
done

if (( ${#missing[@]} > 0 )); then
  echo "ERROR: model-id drift detected between spec files and $TEMPLATE:" >&2
  for m in "${missing[@]}"; do
    echo "  - $m" >&2
  done
  echo "" >&2
  echo "Fix: either add the missing model to $TEMPLATE or correct the spec reference." >&2
  exit 1
fi

echo "Model references consistent: all litellm/<id> references resolve in $TEMPLATE"
