#!/usr/bin/env bash
# Validate cross-spec consistency of model references using tier-based mapping.
#
# Three invariants:
#   1) Template membership: every `litellm/<id>` reference in each spec
#      file registered by review-classify's is_spec_md() must resolve in
#      setup/openclaw.json.template's models array.
#   2) Tier-config consistency: modelTiers in the template must match
#      agents.defaults (expensive=primary, medium=heartbeat+fallback).
#   3) Cron-tier agreement: cron spec files must use the cheap-tier model
#      from modelTiers, and sibling docs must reference "cheap-tier" in
#      their cron-contract sections.
#
# Model tier mapping lives in setup/openclaw.json.template under modelTiers.
# New instances: edit modelTiers + agents.defaults + cron spec model: lines,
# then run this script to verify consistency.
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

# --- Parse modelTiers from template ---------------------------------------

extract_tier() {
  # $1 = tier name (expensive, medium, cheap)
  # Matches "tierName": "model-id" on the same line
  local val
  val="$(grep -E "\"$1\"[[:space:]]*:" "$TEMPLATE" | head -1 | sed -E 's/.*:[[:space:]]*"([^"]+)".*/\1/')"
  if [[ -z "$val" ]]; then
    echo "ERROR: modelTiers.$1 not found in $TEMPLATE" >&2
    exit 1
  fi
  printf '%s' "$val"
}

tier_expensive="$(extract_tier expensive)"
tier_medium="$(extract_tier medium)"
tier_cheap="$(extract_tier cheap)"

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

# --- Invariant 2: tier-config consistency --------------------------------

tier_errors=()

# Check that each tier model exists in the models array
for tier_name in expensive medium cheap; do
  tier_val="$(extract_tier "$tier_name")"
  if ! grep -qx "$tier_val" <<<"$template_ids"; then
    tier_errors+=("modelTiers.$tier_name = $tier_val (not in $TEMPLATE models[].id)")
  fi
done

# Check agents.defaults.model.primary matches expensive tier
primary_model="$(grep -oE '"primary":[[:space:]]*"litellm/[^"]+"' "$TEMPLATE" | grep -oE 'litellm/[^"]+' | head -1)"
if [[ "$primary_model" != "litellm/$tier_expensive" ]]; then
  tier_errors+=("agents.defaults.model.primary = $primary_model, expected litellm/$tier_expensive (expensive tier)")
fi

# Check agents.defaults.model.fallbacks[0] matches medium tier
fallback_model="$(grep -A2 '"fallbacks"' "$TEMPLATE" | grep -oE 'litellm/[A-Za-z0-9._-]+' | head -1)"
if [[ -z "$fallback_model" ]]; then
  tier_errors+=("agents.defaults.model.fallbacks not found in $TEMPLATE")
elif [[ "$fallback_model" != "litellm/$tier_medium" ]]; then
  tier_errors+=("agents.defaults.model.fallbacks[0] = $fallback_model, expected litellm/$tier_medium (medium tier)")
fi

# Check agents.defaults.heartbeat.model matches medium tier
heartbeat_model="$(grep -oE '"model":[[:space:]]*"litellm/[^"]+"' "$TEMPLATE" | tail -1 | grep -oE 'litellm/[^"]+')"
if [[ "$heartbeat_model" != "litellm/$tier_medium" ]]; then
  tier_errors+=("agents.defaults.heartbeat.model = $heartbeat_model, expected litellm/$tier_medium (medium tier)")
fi

# --- Invariant 3: cron-tier agreement -----------------------------------

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

# Cron specs must have concrete model ID matching modelTiers.cheap
cron_errors=()
expected_cron_ref="litellm/$tier_cheap"

for f in "${canonical_cron_sources[@]}"; do
  # Check that the model: line uses the cheap tier model
  cron_model="$(grep -E '^\s+model:\s+litellm/' "$f" | grep -oE 'litellm/[A-Za-z0-9._-]+' | head -1)"
  if [[ -z "$cron_model" ]]; then
    cron_errors+=("$f: no model: litellm/<id> line found")
  elif [[ "$cron_model" != "$expected_cron_ref" ]]; then
    cron_errors+=("$f: model = $cron_model, expected $expected_cron_ref (cheap tier)")
  fi
  # Check tier comment present
  if ! grep -qE '^\s+model:.*# must match modelTiers\.cheap' "$f"; then
    cron_errors+=("$f: missing '# must match modelTiers.cheap' comment on model: line")
  fi
done

# Sibling docs must reference "cheap-tier" in cron-contract sections
sibling_files=(
  "setup/README.md"
  "docs/architecture.md"
  "docs/openclaw-integration.md"
  "docs/heartbeat-checks.md"
)

check_sibling_tier_ref() {
  local spec="$1"
  # All sibling docs must reference tiers, not hardcoded model IDs.
  # Accept either "cheap-tier" prose or "modelTiers.cheap" notation.
  grep -qE 'cheap-tier|modelTiers\.cheap|modelTiers' "$spec"
}

for spec in "${sibling_files[@]}"; do
  [[ -f "$spec" ]] || continue
  if ! check_sibling_tier_ref "$spec"; then
    cron_errors+=("$spec: cron-contract section missing cheap-tier language (should reference tier, not hardcoded model ID)")
  fi
done

# --- Report results ------------------------------------------------------

all_errors=()
if (( ${#missing[@]} > 0 )); then
  all_errors+=("Model-id references missing from $TEMPLATE:")
  for m in "${missing[@]}"; do all_errors+=("  - $m"); done
fi
if (( ${#tier_errors[@]} > 0 )); then
  all_errors+=("Tier-config consistency errors:")
  for t in "${tier_errors[@]}"; do all_errors+=("  - $t"); done
fi
if (( ${#cron_errors[@]} > 0 )); then
  all_errors+=("Cron-tier agreement errors:")
  for c in "${cron_errors[@]}"; do all_errors+=("  - $c"); done
fi

if (( ${#all_errors[@]} > 0 )); then
  echo "ERROR: model reference validation failed:" >&2
  for e in "${all_errors[@]}"; do echo "$e" >&2; done
  echo "" >&2
  echo "Fix: update modelTiers in $TEMPLATE, then ensure cron specs and docs match." >&2
  exit 1
fi

echo "Model references consistent: template membership OK; tier-config OK (expensive=$tier_expensive, medium=$tier_medium, cheap=$tier_cheap); cron specs use cheap tier ($expected_cron_ref)"
