#!/usr/bin/env bash
# ci-local.sh — run exactly what CI runs, in CI's environment, from a laptop.
#
# CI and a developer machine drift in ways that waste review-pipeline cycles:
# a local run misses the image-reward key CI deliberately unsets, doesn't know
# the LLM proxy has one inference slot shared with the homelab CI runner, and
# has no equivalent of the "was this already checked?" workflow gates. This
# script closes that gap by shelling out to the *same* commands
# `.github/workflows/python-validation.yml` and `.github/workflows/e2e.yml`
# run, with the same env.
#
# It intentionally does NOT wrap the compose smoke test
# (tests/smoke/test_compose_round_trip.py, gated by ENABLE_COMPOSE_SMOKE): its
# teardown runs `docker compose down -v`, which on a developer box tears down
# the *local* compose stack's volumes, not a throwaway one. Run it directly if
# you mean to.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

usage() {
  cat <<'EOF'
Usage: scripts/ci-local.sh <mode> [options]

Modes:
  unit          ruff + mypy + pytest tests/unit/, no DATABASE_URL (mirrors
                the python-validation.yml ruff/mypy/pytest-unit jobs).
  db            pytest tests/integration/ tests/regressions/ against Postgres
                (mirrors the pytest-db job). DATABASE_URL defaults to
                postgresql://hml:hml@localhost:5432/hml; export DATABASE_URL
                first to point at a different instance.
  e2e [files…]  pytest tests/e2e/ (or the given files) with e2e.yml's exact
                env. Refuses to start while the homelab e2e.yml run is
                in_progress/queued (the LLM proxy has one inference slot,
                shared with CI) unless --force is given.
  docs          Delegates to `scripts/run-required-checks.sh ci-docs`.
  all           unit, then db, then docs. Does NOT run e2e — e2e costs a
                shared homelab inference slot and wall-clock minutes, so it
                is opt-in even inside "all".

Options:
  --force       (e2e only) start even if e2e.yml is currently running in CI.
  -h, --help    Show this help.

NOTE: this script never runs tests/smoke/test_compose_round_trip.py. That
test's teardown runs `docker compose down -v`, which on a developer machine
tears down your own local compose stack's volumes, not a throwaway one. Run
it yourself, deliberately, if you mean to: ENABLE_COMPOSE_SMOKE=true pytest
tests/smoke/test_compose_round_trip.py -q
EOF
}

log() { echo "[ci-local] $*"; }
fail() {
  echo "[ci-local] ERROR: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command '$1' is not on PATH. Run the devcontainer's post-create step (uv pip install -e '.[dev]') first."
}

run_unit() {
  require_command ruff
  require_command mypy
  require_command pytest

  if [ -n "${DATABASE_URL:-}" ]; then
    log "unsetting DATABASE_URL for this mode — python-validation.yml's pytest-unit job never sets it"
    unset DATABASE_URL
  fi

  log "ruff check app/ tests/ scripts/"
  ruff check app/ tests/ scripts/

  log "mypy app/"
  mypy app/

  log "pytest tests/unit/ -x -q"
  pytest tests/unit/ -x -q
}

run_db() {
  require_command pytest

  export DATABASE_URL="${DATABASE_URL:-postgresql://hml:hml@localhost:5432/hml}"
  export AUTHORIZED_PEERS="${AUTHORIZED_PEERS:-+15550000001,+15550000002}"
  export SIGNAL_ACCOUNT="${SIGNAL_ACCOUNT:-+15550009999}"
  export LLM_PROXY_BASE_URL="${LLM_PROXY_BASE_URL:-http://127.0.0.1:9/v1}"
  export LLM_PROXY_API_KEY="${LLM_PROXY_API_KEY:-placeholder}"
  export REWARD_ARTIFACTS_DIR="${REWARD_ARTIFACTS_DIR:-$(mktemp -d)}"
  mkdir -p "$REWARD_ARTIFACTS_DIR"

  log "DATABASE_URL=$DATABASE_URL"
  log "pytest tests/integration/ tests/regressions/ -q"
  pytest tests/integration/ tests/regressions/ -q
}

_e2e_slot_busy() {
  command -v gh >/dev/null 2>&1 || return 1
  local status
  status="$(gh run list --workflow=e2e.yml --limit 1 --json status --jq '.[0].status' 2>/dev/null || true)"
  [ "$status" = "in_progress" ] || [ "$status" = "queued" ]
}

run_e2e() {
  local force="$1"
  shift
  local -a files=("$@")

  require_command pytest

  if [ "$force" != "true" ] && _e2e_slot_busy; then
    fail "e2e.yml is currently running in CI (in_progress/queued) and the homelab LLM proxy has one inference slot. Wait for it to finish, or pass --force."
  fi

  # OPENAI_API_KEY is deliberately unset, always: generate_reward_image()
  # short-circuits without it, so rewards stay emoji-only, exactly like the
  # CI job. A key set in your shell for other projects would otherwise leak
  # in and incur real image-generation cost.
  unset OPENAI_API_KEY

  export ENABLE_E2E_CONVERSATIONS=true
  export E2E_MAX_LLM_CALLS="${E2E_MAX_LLM_CALLS:-120}"
  export E2E_DEBUG_TURNS="${E2E_DEBUG_TURNS:-true}"
  export DATABASE_URL="${DATABASE_URL:-postgresql://hml:hml@localhost:5432/hml}"
  export LLM_PROXY_API_KEY="${LLM_PROXY_API_KEY:-fake-key}"
  export LLM_PROXY_BASE_URL="${LLM_PROXY_BASE_URL:-https://llm.featherback-mermaid.ts.net/v1}"
  export AUTHORIZED_PEERS="${AUTHORIZED_PEERS:-+15550000001,+15550000002}"
  export SIGNAL_ACCOUNT="${SIGNAL_ACCOUNT:-+15550009999}"
  export REWARD_ARTIFACTS_DIR="${REWARD_ARTIFACTS_DIR:-$(mktemp -d)}"
  mkdir -p "$REWARD_ARTIFACTS_DIR"

  log "DATABASE_URL=$DATABASE_URL"
  if [ "${#files[@]}" -eq 0 ]; then
    log "pytest tests/e2e/ -q -rs"
    pytest tests/e2e/ -q -rs
  else
    log "pytest ${files[*]} -q -rs"
    pytest "${files[@]}" -q -rs
  fi
}

run_docs() {
  "$REPO_ROOT/scripts/run-required-checks.sh" ci-docs
}

run_all() {
  run_unit
  run_db
  run_docs
}

main() {
  local mode="${1:-}"
  local force="false"
  local -a rest=()

  if [ -z "$mode" ]; then
    usage
    exit 1
  fi
  shift || true

  while [ $# -gt 0 ]; do
    case "$1" in
      --force)
        force="true"
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        rest+=("$1")
        shift
        ;;
    esac
  done

  case "$mode" in
    unit)
      run_unit
      ;;
    db)
      run_db
      ;;
    e2e)
      run_e2e "$force" "${rest[@]}"
      ;;
    docs)
      run_docs
      ;;
    all)
      run_all
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "Unknown mode: $mode" >&2
      usage
      exit 1
      ;;
  esac
}

main "$@"
