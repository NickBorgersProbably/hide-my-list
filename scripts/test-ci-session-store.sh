#!/usr/bin/env bash
#
# test-ci-session-store.sh — Unit tests for scripts/ci-session-store.sh.
# Covers the path-naming, validation, and trailer-parse logic that the
# resolve-issue workflow and the v2 review-fixer dispatch depend on.
#
# Runs as a self-contained test: no network, no docker, no LLM.
# Suitable for the pre-merge resume-smoke workflow + local verification.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="${REPO_ROOT}/scripts/ci-session-store.sh"
TEST_ROOT="$(mktemp -d /tmp/ci-session-test.XXXXXX)"
trap 'rm -rf "$TEST_ROOT"' EXIT

export CI_SESSION_ROOT="$TEST_ROOT"

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

# --- home-path ---
assert_eq "home-path codex"   "/home/ci/.codex/sessions" "$("$HELPER" home-path codex)"
assert_eq "home-path claude"  "/home/ci/.claude"         "$("$HELPER" home-path claude)"
assert_exit "home-path bogus rejected" 64 "$HELPER" home-path bogus

# --- host-dir ---
assert_eq "host-dir codex/123/45" \
  "${TEST_ROOT}/codex/123/45" \
  "$("$HELPER" host-dir codex 123 45)"
assert_eq "host-dir claude/9/8" \
  "${TEST_ROOT}/claude/9/8" \
  "$("$HELPER" host-dir claude 9 8)"
assert_exit "host-dir non-numeric issue rejected" 64 "$HELPER" host-dir codex abc 1
assert_exit "host-dir non-numeric run-id rejected" 64 "$HELPER" host-dir codex 1 abc
assert_exit "host-dir bogus agent rejected" 64 "$HELPER" host-dir bogus 1 1

# --- prepare ---
DIR="$("$HELPER" prepare codex 100 200)"
assert_eq "prepare prints host dir" "${TEST_ROOT}/codex/100/200" "$DIR"
if [ -d "$DIR" ]; then
  passes=$((passes + 1))
  printf 'ok    prepare creates dir\n'
else
  failures=$((failures + 1))
  printf 'FAIL  prepare did not create dir\n' >&2
fi
PERM="$(stat -c '%a' "$DIR")"
assert_eq "prepare chmods 0777" "777" "$PERM"

# --- validate ---
assert_exit "validate empty dir fails" 1 "$HELPER" validate codex 100 200

# The codex bind-mount target is now $HOME/.codex/sessions directly,
# so any non-empty dir reflects real session JSONLs from the author run.
# (Pre-fix, the script required a sessions/ subdir to filter out
# config-only dirs written by configure-codex.sh; that's no longer
# relevant since config.toml lives outside the bind-mount.)
echo "session-data" > "$DIR/2026-01-01.jsonl"
assert_exit "validate codex populated dir succeeds" 0 "$HELPER" validate codex 100 200

# Claude validate has the same any-non-empty rule.
CLAUDE_DIR="$("$HELPER" prepare claude 100 200)"
echo "anything" > "$CLAUDE_DIR/projects.placeholder"
assert_exit "validate claude populated dir succeeds" 0 "$HELPER" validate claude 100 200

assert_exit "validate missing dir fails" 1 "$HELPER" validate codex 999 999

# --- pack / unpack round-trip ---
# Mirror the artifact transit path used by codex.yml + review-fixer.yml:
# author packs a populated session dir → upload-artifact ships it →
# fixer downloads + unpacks into a fresh job-local root → validate succeeds.
PACK_DIR="$("$HELPER" prepare codex 300 400)"
echo "session-data" > "$PACK_DIR/2026-01-01.jsonl"

PACK_TAR="$TEST_ROOT/author-session.tgz"
PACK_OUT="$("$HELPER" pack codex 300 400 "$PACK_TAR")"
assert_eq "pack prints out-tar path" "$PACK_TAR" "$PACK_OUT"
if [ -f "$PACK_TAR" ]; then
  passes=$((passes + 1))
  printf 'ok    pack writes tar file\n'
else
  failures=$((failures + 1))
  printf 'FAIL  pack did not write tar at %q\n' "$PACK_TAR" >&2
fi

assert_exit "pack rejects empty session dir" 1 \
  bash -c "CI_SESSION_ROOT='$TEST_ROOT' '$HELPER' pack codex 999 999 '$TEST_ROOT/empty.tgz'"
assert_exit "pack requires out-tar arg" 64 "$HELPER" pack codex 300 400

UNPACK_ROOT="$(mktemp -d /tmp/ci-session-test-unpack.XXXXXX)"
trap 'rm -rf "$TEST_ROOT" "$UNPACK_ROOT"' EXIT
UNPACK_OUT="$(CI_SESSION_ROOT="$UNPACK_ROOT" "$HELPER" unpack codex 300 400 "$PACK_TAR")"
assert_eq "unpack prints host-dir path" "$UNPACK_ROOT/codex/300/400" "$UNPACK_OUT"
if [ -f "$UNPACK_ROOT/codex/300/400/2026-01-01.jsonl" ]; then
  passes=$((passes + 1))
  printf 'ok    unpack restores session contents\n'
else
  failures=$((failures + 1))
  printf 'FAIL  unpack missing expected session file\n' >&2
fi
assert_exit "unpack-extracted dir validates" 0 \
  bash -c "CI_SESSION_ROOT='$UNPACK_ROOT' '$HELPER' validate codex 300 400"

assert_exit "unpack rejects missing tar" 1 "$HELPER" unpack codex 1 1 "/tmp/does-not-exist.tgz"
assert_exit "unpack requires tar-path arg" 64 "$HELPER" unpack codex 1 1

# --- format-trailer ---
assert_eq "format-trailer codex" \
  "Author-Session: codex/12345" \
  "$("$HELPER" format-trailer codex 12345)"
assert_eq "format-trailer claude" \
  "Author-Session: claude/99" \
  "$("$HELPER" format-trailer claude 99)"
assert_exit "format-trailer bogus agent rejected" 64 "$HELPER" format-trailer bogus 1
assert_exit "format-trailer non-numeric run-id rejected" 64 "$HELPER" format-trailer codex abc

# --- parse-trailer ---
body=$'feature\n\nFixes #42\n\nAuthor-Session: codex/12345\n'
assert_eq "parse-trailer codex" $'codex\t12345' "$("$HELPER" parse-trailer "$body")"

body=$'Author-Session: claude/99\nbody continues'
assert_eq "parse-trailer claude" $'claude\t99' "$("$HELPER" parse-trailer "$body")"

body="Author-Session: CODEX/777"
assert_eq "parse-trailer uppercase normalized" $'codex\t777' "$("$HELPER" parse-trailer "$body")"

body="no trailer here"
assert_eq "parse-trailer absent → empty" "" "$("$HELPER" parse-trailer "$body")"

body="Author-Session: bogus/1"
assert_eq "parse-trailer bogus agent → empty" "" "$("$HELPER" parse-trailer "$body")"

body="Author-Session: codex/abc"
assert_eq "parse-trailer non-numeric run-id → empty" "" "$("$HELPER" parse-trailer "$body")"

body="Author-Session: codex"
assert_eq "parse-trailer missing run-id → empty" "" "$("$HELPER" parse-trailer "$body")"

# --- round trip ---
trailer="$("$HELPER" format-trailer codex 555)"
parsed="$("$HELPER" parse-trailer "$trailer")"
assert_eq "round-trip format → parse" $'codex\t555' "$parsed"

# --- summary ---
total=$((passes + failures))
printf '\n%s passes, %s failures (out of %s assertions)\n' "$passes" "$failures" "$total"
[ "$failures" -eq 0 ]
