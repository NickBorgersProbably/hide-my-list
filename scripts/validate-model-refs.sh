#!/usr/bin/env bash
# Validate cross-spec consistency of `litellm/<id>` model references.
#
# Two invariants:
#   1) Template membership: every `litellm/<id>` reference in any spec
#      file must resolve in setup/openclaw.json.template's models array.
#   2) Cross-spec agreement: the canonical cron-model contract lives in
#      setup/cron/reminder-check.md + setup/cron/pull-main.md. Other spec
#      files that describe cron (setup/README.md, docs/architecture.md,
#      docs/openclaw-integration.md, docs/heartbeat-checks.md, HEARTBEAT.md)
#      must only reference model ids from the canonical set. The heartbeat
#      model (from setup/cron/heartbeat-check.md) is also allowed because
#      openclaw-integration.md documents it.
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
if [[ ! -f "$TEMPLATE" ]]; then
  echo "ERROR: $TEMPLATE not found" >&2
  exit 1
fi

extract_litellm_refs() {
  # $1 = file; prints one `litellm/<id>` token per line
  grep -oE 'litellm/[A-Za-z0-9._-]+' "$1" | sort -u
}

# --- Invariant 1: template membership -----------------------------------

template_ids="$(grep -oE '"id":[[:space:]]*"[^"]+"' "$TEMPLATE" | sed -E 's/.*"([^"]+)"$/\1/' | sort -u)"

membership_check_files=(
  "HEARTBEAT.md"
  "docs/architecture.md"
  "docs/openclaw-integration.md"
  "docs/heartbeat-checks.md"
  "setup/README.md"
)
for f in setup/cron/*.md; do
  # Skip caveman-compression backups.
  case "$f" in *.original.md) continue ;; esac
  membership_check_files+=("$f")
done

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
canonical_heartbeat_source="setup/cron/heartbeat-check.md"

for f in "${canonical_cron_sources[@]}" "$canonical_heartbeat_source"; do
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
mapfile -t canonical_heartbeat_refs < <(extract_litellm_refs "$canonical_heartbeat_source")

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
allowed_cron_context=("${canonical_cron_refs[@]}" "${canonical_heartbeat_refs[@]}")

# Sibling spec files that describe cron / heartbeat behavior. Any
# `litellm/<id>` they name must appear in the allowed set above.
sibling_files=(
  "setup/README.md"
  "docs/architecture.md"
  "docs/openclaw-integration.md"
  "docs/heartbeat-checks.md"
  "HEARTBEAT.md"
)

drift=()
for spec in "${sibling_files[@]}"; do
  [[ -f "$spec" ]] || continue
  while IFS= read -r ref; do
    [[ -z "$ref" ]] && continue
    ok=0
    for allowed in "${allowed_cron_context[@]}"; do
      if [[ "$ref" == "$allowed" ]]; then ok=1; break; fi
    done
    if (( ok == 0 )); then
      drift+=("$spec: references $ref, but canonical cron model is $canonical_cron_ref (from ${canonical_cron_sources[*]})")
    fi
  done < <(extract_litellm_refs "$spec")
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

echo "Model references consistent: canonical cron model = $canonical_cron_ref; all sibling specs agree"
