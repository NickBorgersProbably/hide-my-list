#!/usr/bin/env bash
#
# test-issue-pr-claims.sh — Unit tests for scripts/issue-pr-claims.sh.
#
# Covers the claim-detection logic the resolve-issue workflow uses to suppress
# duplicate dispatch. The regex boundary cases matter most: a PR that *cites* an
# issue must not read as a claim on it, and a PR that closes #63 must not read
# as a claim on #6.
#
# Runs as a self-contained test: no network, no docker, no LLM. GitHub calls are
# served by a mock `gh` injected via GH_BIN.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="${REPO_ROOT}/scripts/issue-pr-claims.sh"
TEST_ROOT="$(mktemp -d /tmp/issue-pr-claims-test.XXXXXX)"
trap 'rm -rf "$TEST_ROOT"' EXIT

failures=0
passes=0

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    passes=$((passes + 1))
    printf 'ok    %s\n' "$label"
  else
    failures=$((failures + 1))
    printf 'FAIL  %s\n      expected: %q\n      actual:   %q\n' \
      "$label" "$expected" "$actual" >&2
  fi
}

assert_exit() {
  local label="$1" expected="$2"
  shift 2
  local actual=0
  "$@" >/dev/null 2>&1 || actual=$?
  if [ "$expected" = "$actual" ]; then
    passes=$((passes + 1))
    printf 'ok    %s (exit=%s)\n' "$label" "$actual"
  else
    failures=$((failures + 1))
    printf 'FAIL  %s\n      expected exit: %s\n      actual exit:   %s\n' \
      "$label" "$expected" "$actual" >&2
  fi
}

# --- mock gh -----------------------------------------------------------------
# Serves fixtures from $TEST_ROOT: issue-state.txt, prs-open.json, prs-merged.json.

MOCK_GH="${TEST_ROOT}/gh"
cat > "$MOCK_GH" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  issue)
    cat "${FIXTURE_DIR}/issue-state.txt"
    ;;
  pr)
    state=""
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "--state" ]; then state="$2"; fi
      shift
    done
    cat "${FIXTURE_DIR}/prs-${state}.json"
    ;;
  *)
    echo "mock gh: unhandled: $*" >&2
    exit 1
    ;;
esac
MOCK
chmod +x "$MOCK_GH"
export GH_BIN="$MOCK_GH"
export FIXTURE_DIR="$TEST_ROOT"

set_fixtures() {
  printf '%s\n' "$1" > "${TEST_ROOT}/issue-state.txt"
  printf '%s\n' "$2" > "${TEST_ROOT}/prs-open.json"
  printf '%s\n' "$3" > "${TEST_ROOT}/prs-merged.json"
}

# --- no claim ----------------------------------------------------------------
set_fixtures "OPEN" '[]' '[]'
assert_exit "unclaimed issue exits 1" 1 "$HELPER" is-claimed 629

# --- open PR claims the issue ------------------------------------------------
set_fixtures "OPEN" '[{"number":639,"body":"Resolves #629\n\nSummary."}]' '[]'
assert_exit "open PR claim exits 0" 0 "$HELPER" is-claimed 629
assert_eq "open PR claim names the PR" \
  "open PR(s) already target issue #629: 639" \
  "$("$HELPER" is-claimed 629)"

# --- merged PR already resolved it -------------------------------------------
set_fixtures "OPEN" '[]' '[{"number":635,"body":"Closes #631."}]'
assert_eq "merged PR claim names the PR" \
  "merged PR(s) already resolved issue #631: 635" \
  "$("$HELPER" is-claimed 631)"

# --- closed issue short-circuits ---------------------------------------------
set_fixtures "CLOSED" '[]' '[]'
assert_eq "closed issue is claimed" \
  "issue #632 is CLOSED; nothing to dispatch" \
  "$("$HELPER" is-claimed 632)"

# --- citation is not a claim -------------------------------------------------
# PR #635's real body: "Closes #631. Root cause of the alert ... is #629."
# #631 is claimed; #629 is only cited and must stay dispatchable.
set_fixtures "OPEN" \
  '[{"number":635,"body":"Closes #631. Root cause of the alert that prompted this is #629."}]' \
  '[]'
assert_exit "closing keyword is a claim" 0 "$HELPER" is-claimed 631
assert_exit "bare citation is not a claim" 1 "$HELPER" is-claimed 629

# --- numeric boundary --------------------------------------------------------
set_fixtures "OPEN" '[{"number":700,"body":"Fixes #631"}]' '[]'
assert_exit "prefix number is not a claim" 1 "$HELPER" is-claimed 63
assert_exit "exact number is a claim" 0 "$HELPER" is-claimed 631

# --- keyword variants --------------------------------------------------------
for kw in Closes closes Closed Close Fixes Fixed Fix Resolves Resolved Resolve; do
  set_fixtures "OPEN" "[{\"number\":701,\"body\":\"${kw} #500\"}]" '[]'
  assert_exit "keyword '${kw}' is a claim" 0 "$HELPER" is-claimed 500
done

set_fixtures "OPEN" '[{"number":702,"body":"Fixed: #500"}]' '[]'
assert_exit "keyword with colon is a claim" 0 "$HELPER" is-claimed 500

set_fixtures "OPEN" '[{"number":703,"body":"See #500 for context"}]' '[]'
assert_exit "non-closing verb is not a claim" 1 "$HELPER" is-claimed 500

# --- null body does not crash ------------------------------------------------
set_fixtures "OPEN" '[{"number":704,"body":null}]' '[]'
assert_exit "null PR body is tolerated" 1 "$HELPER" is-claimed 500

# --- duplicates ---------------------------------------------------------------
set_fixtures "OPEN" \
  '[{"number":639,"body":"Resolves #632"},{"number":638,"body":"Closes #632"},{"number":637,"body":"Resolves #631"}]' \
  '[]'
assert_eq "duplicates lists every claiming PR" \
  "639 638" \
  "$("$HELPER" duplicates 632 | tr '\n' ' ' | sed 's/ $//')"
assert_eq "duplicates excludes other issues" \
  "637" \
  "$("$HELPER" duplicates 631)"

# --- issue-state --------------------------------------------------------------
set_fixtures "CLOSED" '[]' '[]'
assert_eq "issue-state reports state" "CLOSED" "$("$HELPER" issue-state 632)"

# --- argument validation ------------------------------------------------------
assert_exit "missing issue number rejected" 2 "$HELPER" is-claimed
assert_exit "non-numeric issue rejected" 2 "$HELPER" is-claimed abc
assert_exit "unknown subcommand rejected" 2 "$HELPER" bogus 1
assert_exit "--repo without value rejected" 2 "$HELPER" is-claimed 1 --repo
assert_exit "help exits 0" 0 "$HELPER" --help

# --- summary ------------------------------------------------------------------
printf '\n%s passed, %s failed\n' "$passes" "$failures"
[ "$failures" -eq 0 ]
