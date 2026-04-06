#!/usr/bin/env bash
set -euo pipefail

# Validates cross-file references in GitHub Actions workflows:
# 1. Local composite action references (uses: ./.github/actions/X) resolve to real directories
# 2. workflow_run trigger names match actual workflow name: fields
# 3. Composite action directories contain action.yml

ERRORS=0
REPO_ROOT="$(git rev-parse --show-toplevel)"

echo "=== Checking local composite action references ==="

# Find all uses: ./.github/actions/X references in workflow files
while IFS= read -r line; do
  file="${line%%:*}"
  # Extract the action path from uses: ./.github/actions/X
  action_path=$(echo "$line" | grep -oP 'uses:\s*\K\./.github/actions/[^\s]+' || true)
  if [ -z "$action_path" ]; then
    continue
  fi

  # Resolve to absolute path
  abs_path="${REPO_ROOT}/${action_path#./}"

  if [ ! -d "$abs_path" ]; then
    echo "ERROR: ${file}: references action '${action_path}' but directory does not exist"
    ERRORS=$((ERRORS + 1))
  elif [ ! -f "${abs_path}/action.yml" ] && [ ! -f "${abs_path}/action.yaml" ]; then
    echo "ERROR: ${file}: references action '${action_path}' but no action.yml found in directory"
    ERRORS=$((ERRORS + 1))
  fi
done < <(grep -rn 'uses:.*\./.github/actions/' "${REPO_ROOT}/.github/workflows/" 2>/dev/null || true)

echo "=== Checking workflow_run trigger name consistency ==="

# Build a map of actual workflow names from name: fields
declare -A workflow_names
while IFS= read -r wf_file; do
  name=$(grep -m1 -oP '^name:\s*\K.*' "$wf_file" || true)
  if [ -n "$name" ]; then
    workflow_names["$name"]=1
  fi
done < <(find "${REPO_ROOT}/.github/workflows" -name '*.yml' -o -name '*.yaml')

# Find all workflow_run trigger blocks and extract referenced workflow names
for wf_file in "${REPO_ROOT}"/.github/workflows/*.yml; do
  [ -f "$wf_file" ] || continue
  wf_basename=$(basename "$wf_file")

  # Extract workflow names from workflow_run.workflows arrays
  # Handles both inline ["Name1", "Name2"] and multi-line - "Name" formats
  in_workflow_run=false
  in_workflows=false
  mapfile -t workflow_lines < "$wf_file"
  for line in "${workflow_lines[@]}"; do
    # Detect workflow_run: block
    if echo "$line" | grep -qP '^\s*workflow_run:'; then
      in_workflow_run=true
      in_workflows=false
      continue
    fi

    # Exit workflow_run block on next top-level key
    if $in_workflow_run && echo "$line" | grep -qP '^\s{0,2}\S' && ! echo "$line" | grep -qP '^\s*(workflows|types|branches)'; then
      in_workflow_run=false
      in_workflows=false
      continue
    fi

    if $in_workflow_run; then
      # Detect workflows: key (inline or block)
      if echo "$line" | grep -qP '^\s*workflows:'; then
        in_workflows=true
        # Check for inline format: workflows: ["Name1", "Name2"]
        if echo "$line" | grep -qP '\['; then
          inline_names=$(echo "$line" | grep -oP '"[^"]+"' | tr -d '"' || true)
          if [ -n "$inline_names" ]; then
            while IFS= read -r ref_name; do
              if [ -z "${workflow_names[$ref_name]+x}" ]; then
                echo "ERROR: ${wf_basename}: workflow_run references '${ref_name}' but no workflow with that name exists"
                ERRORS=$((ERRORS + 1))
              fi
            done <<< "$inline_names"
          fi
          in_workflows=false
        fi
        continue
      fi

      # Multi-line workflow list items: - "Name"
      if $in_workflows && echo "$line" | grep -qP '^\s+-\s'; then
        ref_name=$(echo "$line" | grep -oP '"\K[^"]+' || true)
        if [ -n "$ref_name" ] && [ -z "${workflow_names[$ref_name]+x}" ]; then
          echo "ERROR: ${wf_basename}: workflow_run references '${ref_name}' but no workflow with that name exists"
          ERRORS=$((ERRORS + 1))
        fi
        continue
      fi

      # Exit workflows block on next key at same or higher level
      if $in_workflows && echo "$line" | grep -qP '^\s{4}\S' && ! echo "$line" | grep -qP '^\s+-'; then
        in_workflows=false
      fi
    fi
  done
done

echo ""
if [ "$ERRORS" -gt 0 ]; then
  echo "FAILED: ${ERRORS} cross-file reference error(s) found"
  exit 1
fi

echo "All cross-file workflow references are valid"
