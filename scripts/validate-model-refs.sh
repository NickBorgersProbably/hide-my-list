#!/usr/bin/env bash
# Validate cross-spec consistency of `litellm/<id>` model references.
#
# Two invariants:
#   1) Template membership: every `litellm/<id>` reference in each spec
#      file registered by review-classify's is_spec_md() must resolve in
#      setup/openclaw.json.template's models array.
#   2) Cross-spec agreement: the canonical cron-model contract lives in
#      setup/cron/reminder-check.md + setup/cron/pull-main.md. Other docs'
#      cron-contract sections must match that canonical model.
#
# Background: model ids drift across spec files on every rename. Earlier
# version of this check only enforced (1), so it silently passed when
# setup/README.md said `litellm/gemma4` while cron specs said
# `litellm/claude-haiku-4-5`. The cross-spec check catches that.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

TEMPLATE="setup/openclaw.json.template"
CLASSIFIER=".github/actions/review-classify/action.yml"
if [[ ! -f "$TEMPLATE" ]]; then
  echo "ERROR: $TEMPLATE not found" >&2
  exit 1
fi
if [[ ! -f "$CLASSIFIER" ]]; then
  echo "ERROR: $CLASSIFIER not found" >&2
  exit 1
fi

extract_litellm_refs() {
  # $1 = file; prints one `litellm/<id>` token per line
  grep -oE 'litellm/[A-Za-z0-9._-]+' "$1" | sort -u
}

extract_classifier_spec_patterns() {
  awk '/^[[:space:]]*is_spec_md\(\)/{in_fn=1; next} in_fn && /^[[:space:]]*\}/{in_fn=0} in_fn' "$CLASSIFIER" \
    | grep -oE '[A-Za-z0-9./*-]+\.md' \
    | sort -u
}

build_membership_check_files() {
  local pattern path

  shopt -s nullglob
  while IFS= read -r pattern; do
    [[ -z "$pattern" ]] && continue
    case "$pattern" in
      *'*'*)
        for path in $pattern; do
          [[ -f "$path" ]] && printf '%s\n' "$path"
        done
        ;;
      *)
        [[ -f "$pattern" ]] && printf '%s\n' "$pattern"
        ;;
    esac
  done < <(extract_classifier_spec_patterns)
  shopt -u nullglob
}

extract_cron_contract_refs() {
  local spec="$1"

  case "$spec" in
    "setup/README.md")
      grep -F 'Both jobs run as isolated cron sessions' "$spec" | grep -oE 'litellm/[A-Za-z0-9._-]+' | sort -u
      ;;
    "docs/architecture.md")
      grep -F "Both \`reminder-check\` and \`pull-main\` use" "$spec" | grep -oE 'litellm/[A-Za-z0-9._-]+' | sort -u
      ;;
    "docs/openclaw-integration.md")
      grep -F "**Current registration contract:** Both \`reminder-check\` and \`pull-main\` run as isolated cron sessions" "$spec" \
        | grep -oE 'litellm/[A-Za-z0-9._-]+' \
        | sort -u
      ;;
    "docs/heartbeat-checks.md")
      awk '/^### 2\. Cron Job Health/{in_section=1} /^### 3\./{in_section=0} in_section {print}' "$spec" \
        | grep -oE 'litellm/[A-Za-z0-9._-]+' \
        | sort -u
      ;;
    *)
      extract_litellm_refs "$spec"
      ;;
  esac
}

# --- Invariant 1: template membership -----------------------------------

template_ids="$(grep -oE '"id":[[:space:]]*"[^"]+"' "$TEMPLATE" | sed -E 's/.*"([^"]+)"$/\1/' | sort -u)"

mapfile -t membership_check_files < <(build_membership_check_files)

if (( ${#membership_check_files[@]} == 0 )); then
  echo "ERROR: no spec markdown files extracted from $CLASSIFIER is_spec_md()" >&2
  exit 1
fi

missing=()
for spec in "${membership_check_files[@]}"; do
  [[ -f "$spec" ]] || continue
  while IFS= read -r ref; do
    [[ -z "$ref" ]] && continue
    id="${ref#litellm/}"
    if ! grep -qx "$id" <<<"$template_ids"; then
      missing+=("$spec: litellm/$id (not in $TEMPLATE models[].id)")
    fi
  done < <(extract_litellm_refs "$spec")
done

# --- Invariant 2: cross-spec cron-model agreement -----------------------

canonical_cron_sources=(
  "setup/cron/reminder-check.md"
  "setup/cron/pull-main.md"
)

for f in "${canonical_cron_sources[@]}"; do
  if [[ ! -f "$f" ]]; then
    echo "ERROR: canonical source missing: $f" >&2
    exit 1
  fi
done

mapfile -t canonical_cron_refs < <(
  for f in "${canonical_cron_sources[@]}"; do
    extract_litellm_refs "$f"
  done | sort -u
)

if (( ${#canonical_cron_refs[@]} == 0 )); then
  echo "ERROR: no litellm/<id> found in canonical cron sources (${canonical_cron_sources[*]})" >&2
  exit 1
fi
if (( ${#canonical_cron_refs[@]} > 1 )); then
  echo "ERROR: canonical cron sources disagree on model id — got: ${canonical_cron_refs[*]}" >&2
  echo "Fix: reminder-check.md and pull-main.md must reference the same litellm/<id>." >&2
  exit 1
fi

canonical_cron_ref="${canonical_cron_refs[0]}"

sibling_files=(
  "setup/README.md"
  "docs/architecture.md"
  "docs/openclaw-integration.md"
  "docs/heartbeat-checks.md"
)

drift=()
for spec in "${sibling_files[@]}"; do
  [[ -f "$spec" ]] || continue
  found_ref=0
  while IFS= read -r ref; do
    [[ -z "$ref" ]] && continue
    found_ref=1
    if [[ "$ref" != "$canonical_cron_ref" ]]; then
      drift+=("$spec: references $ref, but canonical cron model is $canonical_cron_ref (from ${canonical_cron_sources[*]})")
    fi
  done < <(extract_cron_contract_refs "$spec")
  if (( found_ref == 0 )); then
    drift+=("$spec: cron-contract section missing canonical $canonical_cron_ref reference")
  fi
done

if (( ${#missing[@]} > 0 || ${#drift[@]} > 0 )); then
  if (( ${#missing[@]} > 0 )); then
    echo "ERROR: model-id references missing from $TEMPLATE:" >&2
    for m in "${missing[@]}"; do echo "  - $m" >&2; done
  fi
  if (( ${#drift[@]} > 0 )); then
    echo "ERROR: cross-spec cron-model drift:" >&2
    for d in "${drift[@]}"; do echo "  - $d" >&2; done
  fi
  echo "" >&2
  echo "Fix: either correct the drifted reference or update the canonical sources (setup/cron/*.md)." >&2
  exit 1
fi

echo "Model references consistent: classifier-listed spec refs resolve in template; canonical cron model = $canonical_cron_ref"
