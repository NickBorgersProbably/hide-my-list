#!/usr/bin/env bash
#
# ci-session-store.sh — manage agent author-session storage so the v2
# review pipeline's fixer can resume the original resolve-issue author.
#
# Background: the fixer resumes the author (Codex or Claude Code) so it
# can revisit structural decisions in light of reviewer feedback. Resume
# requires the agent's session state (`~/.codex` or `~/.claude` on the
# author run) to reach the fixer's container.
#
# Transit model: GitHub Actions artifact. The author run packs its
# session dir with `pack`, uploads it as
# `author-session-<agent>-<run-id>`, and writes an
# `Author-Session: <agent>/<run-id>` trailer into the PR body. The
# fixer parses the trailer, downloads the artifact from the original
# run, and `unpack`s it into a fresh job-local dir that's bind-mounted
# back into the resume container.
#
# Job-local dirs (default `${RUNNER_TEMP:-/tmp}/ci-sessions/...`) avoid
# any reliance on persistent host paths — homelab runners are
# ephemeral, so cross-run state must travel through the artifact, not
# through the host filesystem. Per-(agent, issue, run-id) layout
# preserves the per-run isolation that prevents `codex exec resume
# --last` from picking up state from a prior `/autoresolve` attempt.
#
# Subcommands:
#   home-path <agent>                       — print container path to
#                                              mount onto (codex:
#                                              /home/ci/.codex/sessions,
#                                              claude: /home/ci/.claude).
#                                              Codex is scoped to the
#                                              sessions subdir to avoid
#                                              shadowing the standalone
#                                              install at
#                                              /home/ci/.codex/packages/.
#   host-dir <agent> <issue> <run-id>       — print job-local path
#                                              under CI_SESSION_ROOT
#   prepare <agent> <issue> <run-id>        — mkdir + chmod 0777
#                                              (UID 1000 inside the
#                                              container needs write
#                                              access regardless of
#                                              host ownership)
#   validate <agent> <issue> <run-id>       — non-zero exit if dir
#                                              missing or empty after
#                                              an author/unpack run
#   pack <agent> <issue> <run-id> <out-tar> — tar.gz the session dir
#                                              for upload-artifact;
#                                              prints out-tar
#   unpack <agent> <issue> <run-id> <tar>   — prepare dir + extract
#                                              tar into it; prints
#                                              host-dir path
#   format-trailer <agent> <run-id>         — print the canonical PR
#                                              body trailer line
#   parse-trailer <pr-body>                 — print "<agent>\t<run-id>"
#                                              (TAB-separated) extracted
#                                              from `Author-Session:`
#                                              PR trailer; empty if
#                                              absent or malformed
#
# Default root is `${RUNNER_TEMP:-/tmp}/ci-sessions`; override via
# `CI_SESSION_ROOT` for tests or alternate layouts.

set -euo pipefail

ci_session_container_home_path() {
  local agent="$1"
  case "$agent" in
    # Codex CLI 0.125+ installs its standalone runtime under
    # $HOME/.codex/packages/standalone/, with the wrapper at
    # $HOME/.local/bin/codex symlinking into that tree. Bind-mounting an
    # empty dir on top of $HOME/.codex breaks the symlink target and
    # `command -v codex` then fails. Scope the mount to the sessions
    # subdir — that's the only thing that needs to persist between the
    # author and fixer runs.
    codex) printf '%s\n' "/home/ci/.codex/sessions" ;;
    # Claude Code installs the binary at $HOME/.local/bin/claude, so
    # mounting $HOME/.claude does not shadow the runtime. Sessions live
    # under $HOME/.claude/projects, but other state (settings.json,
    # plugins/, tmp/) is recreated on demand by the CLI.
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
  local root="${CI_SESSION_ROOT:-${RUNNER_TEMP:-/tmp}/ci-sessions}"
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
  # The codex bind-mount target is now $HOME/.codex/sessions directly,
  # so any non-empty dir here means real session JSONLs are present —
  # no separate config-vs-session distinction to make.
}

ci_session_pack() {
  local agent="$1" issue="$2" run_id="$3" out_tar="${4:-}"
  if [ -z "$out_tar" ]; then
    printf 'ci-session-store: pack requires <out-tar> path\n' >&2
    return 64
  fi
  ci_session_validate "$agent" "$issue" "$run_id"
  local dir
  dir="$(ci_session_host_dir "$agent" "$issue" "$run_id")"
  mkdir -p "$(dirname "$out_tar")"
  tar -czf "$out_tar" -C "$dir" .
  printf '%s\n' "$out_tar"
}

ci_session_unpack() {
  local agent="$1" issue="$2" run_id="$3" tar_path="${4:-}"
  if [ -z "$tar_path" ]; then
    printf 'ci-session-store: unpack requires <tar-path>\n' >&2
    return 64
  fi
  if [ ! -f "$tar_path" ]; then
    printf 'ci-session-store: tar not found: %q\n' "$tar_path" >&2
    return 1
  fi
  local dir
  dir="$(ci_session_prepare "$agent" "$issue" "$run_id")"
  tar -xzf "$tar_path" -C "$dir"
  printf '%s\n' "$dir"
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
  sed -n '3,60p' "$0"
}

main() {
  local sub="${1:-}"; shift || true
  case "$sub" in
    home-path) ci_session_container_home_path "$@" ;;
    host-dir) ci_session_host_dir "$@" ;;
    prepare) ci_session_prepare "$@" ;;
    validate) ci_session_validate "$@" ;;
    pack) ci_session_pack "$@" ;;
    unpack) ci_session_unpack "$@" ;;
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
