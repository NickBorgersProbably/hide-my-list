#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

usage() {
  cat <<'EOF'
Usage:
  scripts/run-required-checks.sh --mode <pre-commit|pre-push|ci> [--check <scripts|docs|workflows>]... [-- FILE...]

Modes:
  pre-commit  Run staged-file checks for the supplied files.
  pre-push    Run push-blocking checks for the supplied files.
  ci          Run the CI-required checks for the requested categories.

If --check is omitted in pre-commit/pre-push mode, categories are inferred from FILE paths.
EOF
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

require_command() {
  local cmd=$1
  if ! command -v "$cmd" >/dev/null 2>&1; then
    fail "required command '$cmd' is not installed. Re-run setup/bootstrap.sh or use the devcontainer before pushing."
  fi
}

declare -a requested_checks=()
declare -a files=()
mode=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --mode)
      [ "$#" -ge 2 ] || fail "--mode requires a value"
      mode=$2
      shift 2
      ;;
    --check)
      [ "$#" -ge 2 ] || fail "--check requires a value"
      requested_checks+=("$2")
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    --)
      shift
      files+=("$@")
      break
      ;;
    *)
      fail "unknown argument: $1"
      ;;
  esac
done

[ -n "$mode" ] || fail "--mode is required"

case "$mode" in
  pre-commit|pre-push|ci)
    ;;
  *)
    fail "unsupported mode: $mode"
    ;;
esac

has_check() {
  local target=$1
  local item
  for item in "${requested_checks[@]}"; do
    if [ "$item" = "$target" ]; then
      return 0
    fi
  done
  return 1
}

add_check() {
  local target=$1
  has_check "$target" || requested_checks+=("$target")
}

is_doc_file() {
  case "$1" in
    docs/*.md|design/*.md|setup/*.md|README.md|AGENTS.md)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

is_workflow_trigger_file() {
  case "$1" in
    .github/workflows/*.yml|.github/workflows/*.yaml|.github/actions/*|.github/actionlint.yaml|.yamllint|scripts/validate-workflow-refs.sh|scripts/validate-gh-cli-usage.sh)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

if [ "${#requested_checks[@]}" -eq 0 ] && [ "$mode" != "ci" ]; then
  for file in "${files[@]}"; do
    case "$file" in
      scripts/*.sh)
        add_check "scripts"
        ;;
    esac

    if is_doc_file "$file"; then
      add_check "docs"
    fi

    if is_workflow_trigger_file "$file"; then
      add_check "workflows"
    fi
  done
fi

if [ "${#requested_checks[@]}" -eq 0 ]; then
  echo "No required checks to run for this invocation."
  exit 0
fi

declare -a changed_script_files=()
declare -a changed_doc_files=()
declare -a changed_workflow_files=()

for file in "${files[@]}"; do
  case "$file" in
    scripts/*.sh)
      changed_script_files+=("$file")
      ;;
  esac

  if is_doc_file "$file"; then
    changed_doc_files+=("$file")
  fi

  case "$file" in
    .github/workflows/*.yml|.github/workflows/*.yaml)
      changed_workflow_files+=("$file")
      ;;
  esac
done

declare -a all_script_files=()
declare -a all_workflow_files=()
shopt -s nullglob
all_script_files=(scripts/*.sh)
all_workflow_files=(.github/workflows/*.yml .github/workflows/*.yaml)
shopt -u nullglob

run_shellcheck() {
  local scope=$1
  shift
  local -a script_files=("$@")

  [ "${#script_files[@]}" -gt 0 ] || return 0
  require_command shellcheck
  echo "=== shellcheck (${scope}) ==="
  shellcheck "${script_files[@]}"
}

check_script_permissions() {
  local script_file
  [ "${#all_script_files[@]}" -gt 0 ] || return 0

  echo "=== executable bit check (scripts/*.sh) ==="
  for script_file in "${all_script_files[@]}"; do
    if [ ! -x "$script_file" ]; then
      echo "ERROR: $script_file is not executable"
      return 1
    fi
  done
}

check_broken_links() {
  echo "=== broken internal link check ==="

  python3 - <<'PY'
import re
import sys
from pathlib import Path

repo_root = Path.cwd()
targets = []
for pattern in ("docs/**/*.md", "design/**/*.md", "setup/**/*.md"):
    targets.extend(sorted(repo_root.glob(pattern)))
for rel in ("README.md", "AGENTS.md"):
    path = repo_root / rel
    if path.exists():
        targets.append(path)

link_re = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
skip_prefixes = ("http://", "https://", "mailto:", "tel:", "data:")
errors = []

for md_path in targets:
    text = md_path.read_text(encoding="utf-8")
    for raw_target in link_re.findall(text):
        target = raw_target.strip()
        if not target or target.startswith(skip_prefixes) or target.startswith("#"):
            continue
        resolved = (md_path.parent / target.split("#", 1)[0]).resolve()
        if not resolved.exists():
            if resolved.is_relative_to(repo_root):
                display = resolved.relative_to(repo_root)
            else:
                display = resolved
            errors.append(f"BROKEN LINK in {md_path.relative_to(repo_root)}: {target} (resolved to {display})")

if errors:
    for error in errors:
        print(error)
    print(f"FAILED: {len(errors)} broken internal link(s) found")
    sys.exit(1)

print("No broken internal links found")
PY
}

run_mermaid_checks() {
  local scope=$1
  shift
  local -a doc_files=("$@")

  if [ "${#doc_files[@]}" -eq 0 ]; then
    echo "No markdown files matched for Mermaid validation in ${scope}."
    return 0
  fi

  echo "=== Mermaid rendering lint (${scope}) ==="
  ./scripts/lint-mermaid-rendering.sh "${doc_files[@]}"

  echo "=== Mermaid syntax validation (${scope}) ==="
  ./scripts/validate-mermaid.sh "${doc_files[@]}"
}

run_yamllint() {
  local scope=$1
  shift
  local -a workflow_files=("$@")

  [ "${#workflow_files[@]}" -gt 0 ] || return 0
  require_command yamllint
  echo "=== yamllint (${scope}) ==="
  yamllint -c .yamllint "${workflow_files[@]}"
}

run_actionlint() {
  require_command actionlint
  [ "${#all_workflow_files[@]}" -gt 0 ] || return 0
  echo "=== actionlint ==="
  actionlint "${all_workflow_files[@]}"
}

run_workflow_reference_checks() {
  echo "=== workflow reference validation ==="
  ./scripts/validate-workflow-refs.sh
}

run_gh_cli_usage_checks() {
  echo "=== workflow gh cli usage validation ==="
  ./scripts/validate-gh-cli-usage.sh
}

run_scripts_checks() {
  case "$mode" in
    pre-commit)
      run_shellcheck "staged scripts" "${changed_script_files[@]}"
      ;;
    pre-push|ci)
      run_shellcheck "scripts/*.sh" "${all_script_files[@]}"
      check_script_permissions
      ;;
  esac
}

run_docs_checks() {
  case "$mode" in
    pre-commit)
      run_mermaid_checks "staged docs" "${changed_doc_files[@]}"
      ;;
    pre-push|ci)
      check_broken_links
      run_mermaid_checks "changed docs" "${changed_doc_files[@]}"
      ;;
  esac
}

run_workflow_checks() {
  case "$mode" in
    pre-commit)
      run_yamllint "staged workflows" "${changed_workflow_files[@]}"
      ;;
    pre-push|ci)
      run_yamllint "all workflows" "${all_workflow_files[@]}"
      run_actionlint
      run_workflow_reference_checks
      run_gh_cli_usage_checks
      ;;
  esac
}

for check in "${requested_checks[@]}"; do
  case "$check" in
    scripts)
      run_scripts_checks
      ;;
    docs)
      run_docs_checks
      ;;
    workflows)
      run_workflow_checks
      ;;
    *)
      fail "unsupported check: $check"
      ;;
  esac
done
