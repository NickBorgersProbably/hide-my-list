#!/usr/bin/env bash
# Validate cross-spec consistency of model references using tier-based mapping.
#
# Three invariants:
#   1) Template membership: every `litellm/<id>` reference in each spec
#      file registered by review-classify's is_spec_md() must resolve in
#      setup/openclaw.json.template's models array.
#   2) Tier-config consistency: setup/model-tiers.json must match
#      agents.defaults where tiers still apply (expensive=primary), optional
#      prompt-surface knobs must stay out of the baseline template, and the
#      disabled built-in heartbeat block must remain internally valid.
#   3) Cron-tier agreement: routine cron spec files must use the cheap-tier
#      model from setup/model-tiers.json, janitor must stay on a configured
#      decoupled model, and sibling docs' cron-contract sections must point
#      back to the canonical setup/cron specs instead of drifting.
#
# Model tier mapping is repo metadata, not OpenClaw runtime config.
# New instances: edit setup/model-tiers.json + agents.defaults + routine cron
# spec model lines, then run this script to verify consistency. Built-in
# heartbeat is disabled; the production heartbeat is a cheap-tier cron in
# setup/cron/heartbeat.md. Weekly janitor is intentionally decoupled from the
# cheap tier.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: required command 'jq' is not installed" >&2
  exit 1
fi

TEMPLATE="setup/openclaw.json.template"
TIER_MAP="setup/model-tiers.json"
CLASSIFIER=".github/actions/review-classify/action.yml"
if [[ ! -f "$TEMPLATE" ]]; then
  echo "ERROR: $TEMPLATE not found" >&2
  exit 1
fi
if [[ ! -f "$TIER_MAP" ]]; then
  echo "ERROR: $TIER_MAP not found" >&2
  exit 1
fi
if [[ ! -f "$CLASSIFIER" ]]; then
  echo "ERROR: $CLASSIFIER not found" >&2
  exit 1
fi

# --- Parse model tiers ----------------------------------------------------

extract_tier() {
  # $1 = tier name (expensive, medium, cheap)
  local val
  val="$(jq -er --arg tier "$1" '.[$tier]' "$TIER_MAP")"
  if [[ -z "$val" ]]; then
    echo "ERROR: $TIER_MAP.$1 not found" >&2
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
baseline_errors=()

# Check that each tier model exists in the models array
for tier_name in expensive medium cheap; do
  tier_val="$(extract_tier "$tier_name")"
  if ! grep -qx "$tier_val" <<<"$template_ids"; then
    tier_errors+=("$TIER_MAP.$tier_name = $tier_val (not in $TEMPLATE models[].id)")
  fi
done

# Check agents.defaults.model.primary matches expensive tier
primary_model="$(grep -oE '"primary":[[:space:]]*"litellm/[^"]+"' "$TEMPLATE" | grep -oE 'litellm/[^"]+' | head -1)"
if [[ "$primary_model" != "litellm/$tier_expensive" ]]; then
  tier_errors+=("agents.defaults.model.primary = $primary_model, expected litellm/$tier_expensive (expensive tier)")
fi

reject_template_path() {
  local jq_expr="$1"
  local label="$2"

  if jq -e "$jq_expr" "$TEMPLATE" >/dev/null; then
    baseline_errors+=("$label must not be present in the canonical prompt-footprint baseline")
  fi
}

# Check prompt-footprint baseline excludes optional tool/prompt-surface knobs.
reject_template_path 'has("auth")' 'root auth profiles'
reject_template_path '((.agents.defaults.model // {}) | has("fallbacks"))' 'agents.defaults.model.fallbacks'
reject_template_path '((.agents.defaults // {}) | has("maxConcurrent"))' 'agents.defaults.maxConcurrent'
reject_template_path '((.agents.defaults // {}) | has("subagents"))' 'agents.defaults.subagents'
reject_template_path 'has("messages")' 'messages overrides'
reject_template_path 'has("commands")' 'commands overrides'
reject_template_path '((.skills // {}) | has("install"))' 'skills.install'
reject_template_path '((.channels.signal // {}) | has("defaultTo"))' 'channels.signal.defaultTo'

# Extract the heartbeat block so heartbeat-specific checks cannot be satisfied
# by another `model` or `lightContext` key elsewhere in the template.
heartbeat_block="$(awk '/"heartbeat":[[:space:]]*\{/,/^[[:space:]]*\}/' "$TEMPLATE")"

# Check agents.defaults.heartbeat.every disables the built-in heartbeat.
heartbeat_every="$(grep -oE '"every":[[:space:]]*("[^"]+"|[0-9]+)' <<<"$heartbeat_block" | sed -E 's/.*:[[:space:]]*"?([^",]+)"?/\1/' | head -1)"
if [[ "$heartbeat_every" != "0s" ]]; then
  tier_errors+=("agents.defaults.heartbeat.every must be \"0s\" (built-in heartbeat disabled; use setup/cron/heartbeat.md)")
fi

# Check agents.defaults.heartbeat.model resolves to a configured model if a
# stale live config invokes it before drift repair disables the built-in path.
heartbeat_model="$(grep -oE '"model":[[:space:]]*"litellm/[^"]+"' <<<"$heartbeat_block" | grep -oE 'litellm/[^"]+' | head -1)"
if [[ -z "$heartbeat_model" ]]; then
  tier_errors+=("agents.defaults.heartbeat.model not found in $TEMPLATE")
else
  heartbeat_id="${heartbeat_model#litellm/}"
  if ! grep -qx "$heartbeat_id" <<<"$template_ids"; then
    tier_errors+=("agents.defaults.heartbeat.model = $heartbeat_model (not in $TEMPLATE models[].id)")
  fi
fi

# Check agents.defaults.heartbeat.lightContext = true and reject keys that the
# OpenClaw heartbeat schema does not accept.
if ! grep -qE '"lightContext"[[:space:]]*:[[:space:]]*true' <<<"$heartbeat_block"; then
  tier_errors+=("agents.defaults.heartbeat.lightContext must be true if stale built-in heartbeat config runs")
fi
if grep -qE '"isolatedSession"[[:space:]]*:' <<<"$heartbeat_block"; then
  tier_errors+=("agents.defaults.heartbeat.isolatedSession is not accepted by the OpenClaw config schema")
fi

heartbeat_drift_file="$(mktemp)"
# shellcheck disable=SC2016  # Backticks are literal markdown markers in the regex.
if grep -R -nE '("every"[[:space:]]*:[[:space:]]*0|`every:[[:space:]]*0`|heartbeat[.]every[^`]*\(`0`\)|agents[.]defaults[.]heartbeat[.]every: 0|disabled with `every: 0`)' setup docs DEV-AGENTS.md >"$heartbeat_drift_file"; then
  while IFS= read -r line; do
    tier_errors+=("stale heartbeat disable value: $line")
  done <"$heartbeat_drift_file"
fi
rm -f "$heartbeat_drift_file"

# --- Invariant 3: cron-tier agreement -----------------------------------

cheap_tier_cron_sources=(
  "setup/cron/heartbeat.md"
  "setup/cron/reminder-check.md"
  "setup/cron/reminder-delivery-sweep.md"
  "setup/cron/pull-main.md"
)

decoupled_cron_sources=(
  "setup/cron/janitor.md"
)

canonical_cron_sources=(
  "${cheap_tier_cron_sources[@]}"
  "${decoupled_cron_sources[@]}"
)

for f in "${canonical_cron_sources[@]}"; do
  if [[ ! -f "$f" ]]; then
    echo "ERROR: canonical source missing: $f" >&2
    exit 1
  fi
done

# Cheap-tier cron specs must have concrete model ID matching setup/model-tiers.json.
cron_errors=()
expected_cron_ref="litellm/$tier_cheap"

for f in "${cheap_tier_cron_sources[@]}"; do
  # Check that the model: line uses the cheap tier model
  cron_model="$(grep -E '^\s+model:\s+litellm/' "$f" | grep -oE 'litellm/[A-Za-z0-9._-]+' | head -1)"
  if [[ -z "$cron_model" ]]; then
    cron_errors+=("$f: no model: litellm/<id> line found")
  elif [[ "$cron_model" != "$expected_cron_ref" ]]; then
    cron_errors+=("$f: model = $cron_model, expected $expected_cron_ref (cheap tier)")
  fi
  # Check tier comment present.
  if ! grep -qE '^\s+model:.*# must match setup/model-tiers[.]json cheap' "$f"; then
    cron_errors+=("$f: missing cheap-tier coupling comment on model: line")
  fi
  # Check payload.lightContext: true present (skips bootstrap for isolated cron sessions)
  if ! grep -qE '^\s+lightContext:\s+true' "$f"; then
    cron_errors+=("$f: missing 'lightContext: true' under payload — cron specs must opt into lightweight bootstrap")
  fi
done

for f in "${decoupled_cron_sources[@]}"; do
  cron_model="$(grep -E '^\s+model:\s+litellm/' "$f" | grep -oE 'litellm/[A-Za-z0-9._-]+' | head -1)"
  if [[ -z "$cron_model" ]]; then
    cron_errors+=("$f: no model: litellm/<id> line found")
    continue
  fi
  cron_id="${cron_model#litellm/}"
  if ! grep -qx "$cron_id" <<<"$template_ids"; then
    cron_errors+=("$f: model = $cron_model (not in $TEMPLATE models[].id)")
  fi
  if ! grep -qE '^\s+model:.*# decoupled from modelTiers' "$f"; then
    cron_errors+=("$f: missing decoupled-model comment on model: line")
  fi
  if ! grep -qE '^\s+lightContext:\s+false' "$f"; then
    cron_errors+=("$f: missing 'lightContext: false' under payload — janitor must load full bootstrap")
  fi
done

extract_anchor_window() {
  local file="$1" anchor="$2" line_count="$3" start
  start="$(grep -nF "$anchor" "$file" | head -1 | cut -d: -f1)"
  [[ -n "$start" ]] || return 1
  sed -n "${start},$((start + line_count - 1))p" "$file"
}

check_contract_window() {
  local file="$1" anchor="$2" line_count="$3" required_pattern="$4" forbidden_pattern="$5" label="$6" section
  section="$(extract_anchor_window "$file" "$anchor" "$line_count")" || {
    cron_errors+=("$file: missing expected contract anchor '$anchor'")
    return
  }
  if ! grep -qE "$required_pattern" <<<"$section"; then
    cron_errors+=("$file: $label missing required contract text")
  fi
  if [[ -n "$forbidden_pattern" ]] && grep -qE "$forbidden_pattern" <<<"$section"; then
    cron_errors+=("$file: $label still contains stale hardcoded-model contract text")
  fi
}

check_contract_window \
  "setup/README.md" \
  "## Customizing Model Tiers" \
  25 \
  'setup/cron/|docs/openclaw-integration\.md' \
  'never need updating when models change' \
  "customization section"

# shellcheck disable=SC2016  # backticks are literal grep anchors, not command substitution
check_contract_window \
  "docs/architecture.md" \
  'The routine recurring jobs `heartbeat`, `reminder-check`, `reminder-delivery-sweep`, and `pull-main` use `sessionTarget: isolated`' \
  5 \
  'cheap-tier model|setup/model-tiers\.json|setup/cron/|janitor.*Opus' \
  '' \
  "cron contract section"

check_contract_window \
  "docs/openclaw-integration.md" \
  "**Current registration contract:**" \
  5 \
  'setup/cron/.*setup/model-tiers\.json|setup/model-tiers\.json.*setup/cron/.*janitor|janitor.*decoupled' \
  '' \
  "cron contract section"

check_contract_window \
  "docs/heartbeat-checks.md" \
  "Check via CronList." \
  24 \
  'setup/cron/<name>\.md.*setup/model-tiers\.json|setup/model-tiers\.json.*setup/cron/<name>\.md' \
  'litellm/<cheap-tier model>' \
  "cron contract section"

# Drift-correction contract (Check 2b) must list payload.lightContext
# so heartbeat re-patches jobs back to the lightweight-bootstrap spec.
check_contract_window \
  "docs/heartbeat-checks.md" \
  "Compare + correct these fields:" \
  15 \
  'payload\.lightContext' \
  '' \
  "cron drift-correction allowlist"

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
if (( ${#baseline_errors[@]} > 0 )); then
  all_errors+=("Prompt-footprint baseline errors:")
  for b in "${baseline_errors[@]}"; do all_errors+=("  - $b"); done
fi
if (( ${#cron_errors[@]} > 0 )); then
  all_errors+=("Cron-tier agreement errors:")
  for c in "${cron_errors[@]}"; do all_errors+=("  - $c"); done
fi

if (( ${#all_errors[@]} > 0 )); then
  echo "ERROR: model reference validation failed:" >&2
  for e in "${all_errors[@]}"; do echo "$e" >&2; done
  echo "" >&2
  echo "Fix: update $TIER_MAP, ensure routine cheap-tier cron specs/docs match, keep janitor on a configured decoupled model, keep built-in heartbeat disabled, and keep optional prompt-surface knobs out of the template baseline." >&2
  exit 1
fi

echo "Model references consistent: template membership OK; tier-config OK (expensive=$tier_expensive, medium=$tier_medium, cheap=$tier_cheap, built-in heartbeat every=$heartbeat_every); prompt-footprint baseline OK; routine cheap-tier cron specs use $expected_cron_ref; decoupled cron specs reference configured models"
