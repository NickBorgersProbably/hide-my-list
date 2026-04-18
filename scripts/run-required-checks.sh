#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

readonly SCRIPT_PATH_PATTERN='^(scripts/.*\.sh|setup/.*\.sh|\.githooks/(install-hooks\.sh|pre-commit|pre-push))$'
readonly SCRIPT_TRIGGER_PATH_PATTERN='^(scripts/.*|setup/.*\.sh|\.githooks/.*)$'
readonly DOC_PATH_PATTERN='^(docs/.*\.md|design/.*\.md|setup/.*\.md|README\.md|AGENTS\.md)$'
readonly DOC_HELPER_PATH_PATTERN='^(scripts/check-doc-links\.sh|scripts/lint-mermaid-rendering\.sh|scripts/validate-mermaid\.sh)$'
readonly WORKFLOW_PATH_PATTERN='^(\.github/workflows/.*\.ya?ml|\.github/actions/.*\.ya?ml|\.github/actionlint\.yaml|\.yamllint|\.githooks/(install-hooks\.sh|pre-commit|pre-push)|scripts/validate-gh-cli-usage\.sh|scripts/validate-pr-tests-workflow\.sh|scripts/validate-workflow-refs\.sh)$'
readonly CANONICAL_RUNNER='scripts/run-required-checks.sh'

changed_files=()

usage() {
  cat <<'EOF'
Usage: scripts/run-required-checks.sh <mode>

Modes:
  pre-commit    Fast checks for staged files only.
  pre-push      Required deterministic checks for changed categories.
  ci-scripts    CI-equivalent shell/script validation.
  ci-docs       CI-equivalent documentation validation.
  ci-workflows  CI-equivalent workflow validation.
EOF
}

require_command() {
  local cmd="$1"

  if command -v "$cmd" >/dev/null 2>&1; then
    return 0
  fi

  echo "ERROR: required command '$cmd' is not installed."
  echo "Install the repo devcontainer or the local tooling before committing/pushing."
  exit 1
}

load_changed_files_from_env() {
  if [ -z "${RUN_REQUIRED_CHECKS_CHANGED_FILES:-}" ]; then
    changed_files=()
    return 0
  fi

  mapfile -t changed_files < <(printf '%s\n' "$RUN_REQUIRED_CHECKS_CHANGED_FILES" | sed '/^$/d' | sort -u)
}

load_staged_files() {
  mapfile -t changed_files < <(git diff --cached --name-only | sed '/^$/d' | sort -u)
}

has_changed_path() {
  local pattern="$1"

  if [ "${#changed_files[@]}" -eq 0 ]; then
    return 1
  fi

  printf '%s\n' "${changed_files[@]}" | grep -Eq "$pattern"
}

canonical_runner_changed() {
  has_changed_path "^${CANONICAL_RUNNER}$"
}

filter_existing_files() {
  local path

  for path in "$@"; do
    if [ -f "$path" ]; then
      printf '%s\n' "$path"
    fi
  done
}

collect_matching_changed_files() {
  local pattern="$1"

  if [ "${#changed_files[@]}" -eq 0 ]; then
    return 0
  fi

  printf '%s\n' "${changed_files[@]}" | grep -E "$pattern" || true
}

run_shellcheck() {
  local -a targets=("$@")

  if [ "${#targets[@]}" -eq 0 ]; then
    return 0
  fi

  require_command shellcheck
  echo "=== Linting shell scripts ==="
  shellcheck "${targets[@]}"
}

run_executable_check() {
  local -a targets=("$@")
  local failed=0
  local path

  if [ "${#targets[@]}" -eq 0 ]; then
    return 0
  fi

  echo "=== Checking script permissions ==="
  for path in "${targets[@]}"; do
    if [ ! -x "$path" ]; then
      echo "ERROR: $path is not executable"
      failed=1
    fi
  done

  if [ "$failed" -ne 0 ]; then
    return 1
  fi

  echo "All scripts have execute permission"
}

run_script_validation() {
  local -a script_targets=()
  local -a executable_targets=()

  mapfile -t script_targets < <(filter_existing_files scripts/*.sh setup/*.sh .githooks/install-hooks.sh .githooks/pre-commit .githooks/pre-push)
  mapfile -t executable_targets < <(filter_existing_files scripts/*.sh .githooks/install-hooks.sh .githooks/pre-commit .githooks/pre-push)
  run_shellcheck "${script_targets[@]}"
  run_executable_check "${executable_targets[@]}"
}

run_doc_link_check() {
  require_command python3
  echo "=== Checking for broken internal links ==="
  "$REPO_ROOT/scripts/check-doc-links.sh"
}

build_doc_targets() {
  local -a doc_targets=()

  if [ "${#changed_files[@]}" -eq 0 ] || canonical_runner_changed || has_changed_path "$DOC_HELPER_PATH_PATTERN"; then
    for target in docs design setup README.md AGENTS.md; do
      if [ -e "$target" ]; then
        doc_targets+=("$target")
      fi
    done
    printf '%s\n' "${doc_targets[@]}"
    return 0
  fi

  mapfile -t doc_targets < <(collect_matching_changed_files "$DOC_PATH_PATTERN" | while IFS= read -r path; do
    if [ -f "$path" ]; then
      printf '%s\n' "$path"
    fi
  done)

  printf '%s\n' "${doc_targets[@]}"
}

run_doc_validation() {
  local -a doc_targets=()

  run_doc_link_check
  echo "=== Validating model-id references ==="
  "$REPO_ROOT/scripts/validate-model-refs.sh"
  echo "=== Validating spec catalog consistency ==="
  "$REPO_ROOT/scripts/validate-spec-catalog.sh"
  mapfile -t doc_targets < <(build_doc_targets)

  if [ "${#doc_targets[@]}" -eq 0 ]; then
    echo "No changed doc files to lint"
    echo "No changed doc files to validate"
    return 0
  fi

  echo "=== Linting Mermaid diagrams ==="
  "$REPO_ROOT/scripts/lint-mermaid-rendering.sh" "${doc_targets[@]}"

  echo "=== Validating Mermaid diagrams ==="
  "$REPO_ROOT/scripts/validate-mermaid.sh" "${doc_targets[@]}"
}

run_yamllint() {
  local -a workflow_targets=()

  mapfile -t workflow_targets < <(filter_existing_files .github/workflows/*.yml .github/workflows/*.yaml)
  if [ "${#workflow_targets[@]}" -eq 0 ]; then
    echo "No workflow YAML files found"
    return 0
  fi

  require_command yamllint
  echo "=== Linting workflow YAML ==="
  yamllint -c .yamllint "${workflow_targets[@]}"
}

run_actionlint() {
  local -a workflow_targets=()

  mapfile -t workflow_targets < <(filter_existing_files .github/workflows/*.yml .github/workflows/*.yaml)
  if [ "${#workflow_targets[@]}" -eq 0 ]; then
    echo "No workflow YAML files found"
    return 0
  fi

  require_command actionlint
  echo "=== Running actionlint ==="
  actionlint "${workflow_targets[@]}"
}

run_workflow_validation() {
  run_yamllint
  require_command python3
  echo "=== Validating PR Tests workflow tool ordering ==="
  "$REPO_ROOT/scripts/validate-pr-tests-workflow.sh"
  run_actionlint
  echo "=== Validating cross-file workflow references ==="
  "$REPO_ROOT/scripts/validate-workflow-refs.sh"
  echo "=== Validating GitHub CLI usage in workflows ==="
  "$REPO_ROOT/scripts/validate-gh-cli-usage.sh"
}

run_pre_commit_script_checks() {
  local -a targets=()

  mapfile -t targets < <(collect_matching_changed_files "$SCRIPT_PATH_PATTERN" | while IFS= read -r path; do
    if [ -f "$path" ]; then
      printf '%s\n' "$path"
    fi
  done)

  if [ "${#targets[@]}" -eq 0 ]; then
    return 0
  fi

  run_shellcheck "${targets[@]}"
  run_executable_check "${targets[@]}"
}

run_pre_commit_doc_checks() {
  local -a targets=()

  mapfile -t targets < <(collect_matching_changed_files "$DOC_PATH_PATTERN" | while IFS= read -r path; do
    if [ -f "$path" ]; then
      printf '%s\n' "$path"
    fi
  done)

  if [ "${#targets[@]}" -eq 0 ]; then
    return 0
  fi

  echo "=== Linting Mermaid diagrams for staged docs ==="
  "$REPO_ROOT/scripts/lint-mermaid-rendering.sh" "${targets[@]}"
  echo "=== Validating Mermaid diagrams for staged docs ==="
  "$REPO_ROOT/scripts/validate-mermaid.sh" "${targets[@]}"
}

run_pre_commit_workflow_checks() {
  local -a targets=()

  mapfile -t targets < <(collect_matching_changed_files '^\.github/workflows/.*\.ya?ml$' | while IFS= read -r path; do
    if [ -f "$path" ]; then
      printf '%s\n' "$path"
    fi
  done)

  if [ "${#targets[@]}" -eq 0 ]; then
    return 0
  fi

  require_command yamllint
  echo "=== Linting staged workflow YAML ==="
  yamllint -c .yamllint "${targets[@]}"
}

run_pre_commit() {
  load_staged_files
  if [ "${#changed_files[@]}" -eq 0 ]; then
    exit 0
  fi

  trap 'echo ""; echo "Pre-commit checks failed. Fix the issues above before committing."' ERR
  run_pre_commit_script_checks
  run_pre_commit_doc_checks
  run_pre_commit_workflow_checks
}

run_pre_push() {
  load_changed_files_from_env
  if [ "${#changed_files[@]}" -eq 0 ]; then
    exit 0
  fi

  trap 'echo ""; echo "Pre-push checks failed. Fix the issues above before pushing."' ERR

  if canonical_runner_changed || has_changed_path "$SCRIPT_TRIGGER_PATH_PATTERN"; then
    run_script_validation
  fi

  if canonical_runner_changed || has_changed_path "$DOC_PATH_PATTERN" || has_changed_path "$DOC_HELPER_PATH_PATTERN"; then
    run_doc_validation
  fi

  if canonical_runner_changed || has_changed_path "$WORKFLOW_PATH_PATTERN"; then
    run_workflow_validation
  fi
}

mode="${1:-}"
case "$mode" in
  pre-commit)
    run_pre_commit
    ;;
  pre-push)
    run_pre_push
    ;;
  ci-scripts)
    run_script_validation
    ;;
  ci-docs)
    load_changed_files_from_env
    run_doc_validation
    ;;
  ci-workflows)
    run_workflow_validation
    ;;
  *)
    usage
    exit 1
    ;;
esac
