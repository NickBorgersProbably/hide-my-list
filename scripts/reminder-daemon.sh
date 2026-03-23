#!/usr/bin/env bash
# reminder-daemon.sh — Run scheduled reminder checks locally
#
# This replaces the GitHub Actions cron that previously polled Notion for
# reminders. It runs check-reminders.sh on a fixed interval (default: 5 min)
# and logs output to help with debugging.
#
# Usage:
#   scripts/reminder-daemon.sh            # loop forever, 5 min interval
#   scripts/reminder-daemon.sh --once     # single reminder check
#   scripts/reminder-daemon.sh --interval 120   # override interval seconds
#
# Environment:
#   REMINDER_POLL_INTERVAL — default interval (seconds)
#   REMINDER_LOG_FILE      — where to write logs (/tmp/reminder-daemon.log)
#   REMINDER_PID_FILE      — PID file location (/tmp/reminder-daemon.pid)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-reminders.sh"
LOG_FILE="${REMINDER_LOG_FILE:-/tmp/reminder-daemon.log}"
PID_FILE="${REMINDER_PID_FILE:-/tmp/reminder-daemon.pid}"
HEARTBEAT_FILE="${REMINDER_HEARTBEAT_FILE:-/tmp/reminder-daemon.heartbeat}"
INTERVAL="${REMINDER_POLL_INTERVAL:-300}"
RUN_ONCE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --once)
            RUN_ONCE=true
            shift
            ;;
        --interval)
            if [[ -z "${2:-}" ]]; then
                echo "--interval requires a value" >&2
                exit 1
            fi
            INTERVAL="$2"
            shift 2
            ;;
        --help|-h)
            cat <<USAGE
Usage: $0 [--once] [--interval seconds]
  --once                Run a single reminder check and exit
  --interval <seconds>  Override polling interval (default: ${INTERVAL}s)
USAGE
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

log() {
    local level="$1"; shift
    printf '[%s] [%s] %s\n' "$(date -Iseconds)" "$level" "$*" | tee -a "$LOG_FILE"
}

update_heartbeat() {
    date -Iseconds > "$HEARTBEAT_FILE"
}

cleanup() {
    log INFO "Reminder daemon shutting down (pid=$$)"
    rm -f "$PID_FILE" "$HEARTBEAT_FILE"
}

# Prevent multiple daemons
if [[ "$RUN_ONCE" == false ]]; then
    if [[ -f "$PID_FILE" ]]; then
        OLD_PID=$(cat "$PID_FILE" 2>/dev/null || true)
        if [[ -n "$OLD_PID" ]] && ps -p "$OLD_PID" >/dev/null 2>&1; then
            echo "reminder-daemon already running (pid $OLD_PID)" >&2
            exit 1
        else
            log WARN "Removing stale PID file (was pid=${OLD_PID:-unknown})"
            rm -f "$PID_FILE"
        fi
    fi
    echo "$$" > "$PID_FILE"
    trap cleanup EXIT
    trap 'log WARN "Caught SIGTERM"; exit 0' TERM
    trap 'log WARN "Caught SIGINT"; exit 0' INT
    trap 'log ERROR "Caught SIGHUP"; exit 1' HUP
fi

if [[ ! -x "$CHECK_SCRIPT" ]]; then
    log ERROR "Cannot find check-reminders.sh (expected at $CHECK_SCRIPT)"
    exit 1
fi

log INFO "Reminder daemon starting (pid=$$, interval=${INTERVAL}s, once=${RUN_ONCE})"

run_once() {
    log INFO "Running reminder check"
    if ! "$CHECK_SCRIPT" >> "$LOG_FILE" 2>&1; then
        log ERROR "check-reminders.sh exited with non-zero status"
    else
        log INFO "Reminder check completed successfully"
    fi
    update_heartbeat
}

if [[ "$RUN_ONCE" == true ]]; then
    run_once
    exit 0
fi

# Initial heartbeat before first check
update_heartbeat

while true; do
    run_once
    log INFO "Sleeping ${INTERVAL}s until next check"
    sleep "$INTERVAL"
done
