#!/usr/bin/env bash
#
# ci-session-store.sh — manage agent author-session storage on the
# self-hosted runner host.
#
# Background: the v2 review pipeline now resumes the original
# resolve-issue author (Codex or Claude Code) in the fixer stage so
# the author can revisit structural decisions in light of reviewer
# feedback. Resume requires the agent's session state to persist
# between containers. We bind-mount a per-(agent, issue, run-id)
# host directory into the agent's home dir; this script owns the
# path conventions.
#
# Per-run subdirs (not per-issue) — leftover state from a previous
# /autoresolve attempt would otherwise poison `codex exec resume
# --last`, which picks the newest cwd-filtered session. Isolating
# each run avoids the cleanup-race entirely; old subdirs are pruned
# by scripts/cleanup-ci-sessions.sh against closed/merged PRs.
#
# Subcommands:
#   home-path <agent>                       — print container path to
#                                              mount onto (codex:
#                                              /home/ci/.codex,
#                                              claude: /home/ci/.claude)
#   host-dir <agent> <issue> <run-id>       — print host path to bind
#   prepare <agent> <issue> <run-id>        — mkdir + chmod 0777
#                                              (UID 1000 inside the
#                                              container needs write
#                                              access regardless of
#                                              host ownership)
#   validate <agent> <issue> <run-id>       — non-zero exit if host
#                                              dir missing or empty
#                                              after an author run
#   format-trailer <agent> <run-id>         — print the canonical PR
#                                              body trailer line
#   parse-trailer <pr-body>                 — print "<agent>\t<run-id>"
#                                              (TAB-separated) extracted
#                                              from `Author-Session:`
#                                              PR trailer; empty if
#                                              absent or malformed
#
# Host store root defaults to /srv/ci-sessions; override via
# CI_SESSION_ROOT for tests or alternate runners.

set -euo pipefail

ci_session_container_home_path() {
  local agent="$1"
  case "$agent" in
    codex) printf '%s\n' "/home/ci/.codex" ;;
    claude) printf '%s\n' "/home/ci/.claude" ;;
    *)
      printf 'ci-session-store: unknown agent %q (expected codex|claude)\n' "$agent" >&2
      return 64
      ;;
  esac
}

_ci_session_validate_args() {
  local agent="$1" issue="$2" run_id="$3"
  case "$agent" in codex|claude) ;; *)
    printf 'ci-session-store: unknown agent %q\n' "$agent" >&2; return 64 ;;
  esac
  case "$issue" in '' | *[!0-9]*)
    printf 'ci-session-store: issue must be numeric, got %q\n' "$issue" >&2; return 64 ;;
  esac
  case "$run_id" in '' | *[!0-9]*)
    printf 'ci-session-store: run-id must be numeric, got %q\n' "$run_id" >&2; return 64 ;;
  esac
}

ci_session_host_dir() {
  _ci_session_validate_args "$1" "$2" "$3"
  local root="${CI_SESSION_ROOT:-/srv/ci-sessions}"
  printf '%s\n' "${root}/${1}/${2}/${3}"
}

ci_session_prepare() {
  local dir
  dir="$(ci_session_host_dir "$1" "$2" "$3")"
  mkdir -p "$dir"
  chmod 0777 "$dir"
  printf '%s\n' "$dir"
}

ci_session_validate() {
  local agent="$1"
  local dir
  dir="$(ci_session_host_dir "$agent" "$2" "$3")"
  if [ ! -d "$dir" ]; then
    printf 'ci-session-store: missing %q\n' "$dir" >&2
    return 1
  fi
  if [ -z "$(ls -A "$dir" 2>/dev/null)" ]; then
    printf 'ci-session-store: %q is empty (author did not persist state)\n' "$dir" >&2
    return 1
  fi
  # config.toml is written by .devcontainer/configure-codex.sh before any
  # conversation state exists. A Codex dir that contains only config artefacts
  # is not resumable — `codex exec resume --last` would start a fresh thread.
  # Require non-empty sessions/ to confirm real conversation state was persisted.
  if [ "$agent" = "codex" ]; then
    if [ -z "$(ls -A "${dir}/sessions" 2>/dev/null)" ]; then
      printf 'ci-session-store: %q has no session state (config-only dir; not resumable)\n' "$dir" >&2
      return 1
    fi
  fi
}

ci_session_format_trailer() {
  local agent="$1" run_id="$2"
  case "$agent" in codex|claude) ;; *)
    printf 'ci-session-store: unknown agent %q\n' "$agent" >&2; return 64 ;;
  esac
  case "$run_id" in '' | *[!0-9]*)
    printf 'ci-session-store: run-id must be numeric, got %q\n' "$run_id" >&2; return 64 ;;
  esac
  printf 'Author-Session: %s/%s\n' "$agent" "$run_id"
}

ci_session_parse_trailer() {
  local body="$1" line
  line="$(printf '%s\n' "$body" \
    | grep -Eim1 '^Author-Session:[[:space:]]+(codex|claude)/[0-9]+[[:space:]]*$' \
    || true)"
  [ -n "$line" ] || return 0
  printf '%s\n' "$line" \
    | sed -E 's#^Author-Session:[[:space:]]+(codex|claude)/([0-9]+)[[:space:]]*$#\1\t\2#i' \
    | tr -d '\r' \
    | awk -F'\t' '{ printf "%s\t%s\n", tolower($1), $2 }'
}

usage() {
  sed -n '3,46p' "$0"
}

main() {
  local sub="${1:-}"; shift || true
  case "$sub" in
    home-path) ci_session_container_home_path "$@" ;;
    host-dir) ci_session_host_dir "$@" ;;
    prepare) ci_session_prepare "$@" ;;
    validate) ci_session_validate "$@" ;;
    format-trailer) ci_session_format_trailer "$@" ;;
    parse-trailer) ci_session_parse_trailer "$@" ;;
    -h|--help|help|"") usage; [ -z "$sub" ] && return 64 || return 0 ;;
    *)
      printf 'ci-session-store: unknown subcommand %q\n' "$sub" >&2
      usage >&2
      return 64
      ;;
  esac
}

main "$@"
