#!/usr/bin/env bash
# ci-local.sh — run the commands CI runs, with CI's environment as defaults.
#
# CI and a developer machine drift in ways that waste review-pipeline cycles:
# a local run misses the image-reward key CI deliberately unsets, doesn't know
# the LLM proxy has one inference slot shared with the homelab CI runner, and
# has no equivalent of the "was this already checked?" workflow gates. This
# script closes that gap by shelling out to the *same* commands
# `.github/workflows/python-validation.yml` and `.github/workflows/e2e.yml`
# run, with their env values as defaults (see --help for what may be
# overridden from the shell).
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
  e2e [files…]  pytest tests/e2e/ (or the given files) with e2e.yml's env
                values as defaults. These may be overridden from the shell:
                DATABASE_URL, LLM_PROXY_BASE_URL, LLM_PROXY_API_KEY,
                E2E_MAX_LLM_CALLS, E2E_DEBUG_TURNS, AUTHORIZED_PEERS,
                SIGNAL_ACCOUNT, REWARD_ARTIFACTS_DIR. OPENAI_API_KEY is always
                unset and ENABLE_E2E_CONVERSATIONS is always true.
                The LLM proxy has one inference slot, shared by e2e.yml,
                nightly-evals.yml and model-swap.yml. The script refuses to
                start while any of them has a queued/in_progress run, and
                also refuses when it cannot check (gh missing, not
                authenticated, API error). --force skips the check.
  docs          Delegates to `scripts/run-required-checks.sh ci-docs`.
  all           unit, then db, then docs. Does NOT run e2e — e2e costs a
                shared homelab inference slot and wall-clock minutes, so it
                is opt-in even inside "all".

Options:
  --force       (e2e only) skip the shared inference slot check.
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

# Workflows that share the `homelab-llm-serial` concurrency group, and so the
# LLM proxy's single inference slot. Keep in sync with the `concurrency:`
# blocks in .github/workflows/.
E2E_SLOT_WORKFLOWS=(e2e.yml nightly-evals.yml model-swap.yml)

# Checks every slot-sharing workflow for a queued or in-progress run.
# Returns 0 when the slot is clear, 1 when a run holds or waits for it, and
# 2 when the check itself cannot be completed (gh missing, not authenticated,
# API error, unparseable output). The caller treats 2 like 1: fail closed.
_e2e_slot_check() {
  if ! command -v gh >/dev/null 2>&1; then
    echo "[ci-local] cannot check the shared inference slot: 'gh' is not on PATH." >&2
    return 2
  fi
  local wf count busy=0
  for wf in "${E2E_SLOT_WORKFLOWS[@]}"; do
    if ! count="$(gh run list --workflow="$wf" --limit 20 --json status \
        --jq '[.[] | select(.status == "queued" or .status == "in_progress" or .status == "waiting" or .status == "pending" or .status == "requested")] | length' 2>/dev/null)"; then
      echo "[ci-local] cannot check the shared inference slot: 'gh run list --workflow=$wf' failed (not authenticated, or the API call failed)." >&2
      return 2
    fi
    if ! [[ "$count" =~ ^[0-9]+$ ]]; then
      echo "[ci-local] cannot check the shared inference slot: unexpected 'gh run list --workflow=$wf' output: '$count'." >&2
      return 2
    fi
    if [ "$count" -gt 0 ]; then
      echo "[ci-local] $wf has $count queued/in_progress run(s) in CI." >&2
      busy=1
    fi
  done
  return "$busy"
}

run_e2e() {
  local force="$1"
  shift
  local -a files=("$@")

  require_command pytest

  if [ "$force" = "true" ]; then
    log "--force: skipping the shared inference slot check"
  else
    local slot_rc=0
    _e2e_slot_check || slot_rc=$?
    if [ "$slot_rc" -eq 1 ]; then
      fail "the homelab LLM proxy has one inference slot, shared by ${E2E_SLOT_WORKFLOWS[*]}, and CI holds or is waiting for it. Wait for those runs to finish, or pass --force."
    elif [ "$slot_rc" -ne 0 ]; then
      fail "refusing to start e2e without confirming the shared inference slot is free. Fix 'gh' (install it and run 'gh auth login'), or pass --force if you know the slot is free."
    fi
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
