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

# Prevent multiple daemons
if [[ "$RUN_ONCE" == false ]]; then
    if [[ -f "$PID_FILE" ]]; then
        if ps -p "$(cat "$PID_FILE" 2>/dev/null)" >/dev/null 2>&1; then
            echo "reminder-daemon already running (pid $(cat "$PID_FILE"))" >&2
            exit 1
        else
            rm -f "$PID_FILE"
        fi
    fi
    echo "$$" > "$PID_FILE"
    trap 'rm -f "$PID_FILE"' EXIT
    trap 'rm -f "$PID_FILE"; exit 0' SIGTERM SIGINT
fi

if [[ ! -x "$CHECK_SCRIPT" ]]; then
    echo "Cannot find check-reminders.sh (expected at $CHECK_SCRIPT)" >&2
    exit 1
fi

log INFO "Reminder daemon starting (interval=${INTERVAL}s, once=${RUN_ONCE})"

run_once() {
    if ! "$CHECK_SCRIPT" >> "$LOG_FILE" 2>&1; then
        log ERROR "check-reminders.sh failed"
    fi
}

if [[ "$RUN_ONCE" == true ]]; then
    run_once
    exit 0
fi

while true; do
    run_once
    sleep "$INTERVAL"
done
