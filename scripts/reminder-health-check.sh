#!/usr/bin/env bash
# reminder-health-check.sh — Verify the reminder daemon is alive and healthy
#
# Checks three conditions:
#   1. PID file exists and the process is running
#   2. Log file exists
#   3. Heartbeat file is recent (written within the staleness threshold)
#
# Exit codes:
#   0 — daemon is healthy
#   1 — daemon is unhealthy (details on stderr)
#
# Usage:
#   scripts/reminder-health-check.sh            # check health, print status
#   scripts/reminder-health-check.sh --restart   # restart if unhealthy
#
# Environment:
#   REMINDER_LOG_FILE       — log file path (default: /tmp/reminder-daemon.log)
#   REMINDER_PID_FILE       — PID file path (default: /tmp/reminder-daemon.pid)
#   REMINDER_HEARTBEAT_FILE — heartbeat file path (default: /tmp/reminder-daemon.heartbeat)
#   REMINDER_STALE_SECONDS  — max age of heartbeat before daemon is "stale"
#                             (default: 900, i.e. 15 minutes)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="${REMINDER_LOG_FILE:-/tmp/reminder-daemon.log}"
PID_FILE="${REMINDER_PID_FILE:-/tmp/reminder-daemon.pid}"
HEARTBEAT_FILE="${REMINDER_HEARTBEAT_FILE:-/tmp/reminder-daemon.heartbeat}"
STALE_SECONDS="${REMINDER_STALE_SECONDS:-900}"
DO_RESTART=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --restart)
            DO_RESTART=true
            shift
            ;;
        --help|-h)
            cat <<USAGE
Usage: $0 [--restart]
  --restart  Restart the daemon if the health check fails
USAGE
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

HEALTHY=true
REASONS=()

# Check 1: PID file and running process
if [[ -f "$PID_FILE" ]]; then
    DAEMON_PID=$(cat "$PID_FILE" 2>/dev/null || true)
    if [[ -z "$DAEMON_PID" ]]; then
        HEALTHY=false
        REASONS+=("PID file exists but is empty")
    elif ! ps -p "$DAEMON_PID" >/dev/null 2>&1; then
        HEALTHY=false
        REASONS+=("PID file references pid=$DAEMON_PID but process is not running")
    fi
else
    HEALTHY=false
    REASONS+=("PID file not found at $PID_FILE")
fi

# Check 2: Log file exists
if [[ ! -f "$LOG_FILE" ]]; then
    HEALTHY=false
    REASONS+=("Log file not found at $LOG_FILE")
fi

# Check 3: Heartbeat freshness
if [[ -f "$HEARTBEAT_FILE" ]]; then
    HEARTBEAT_MTIME=$(stat -c %Y "$HEARTBEAT_FILE" 2>/dev/null || stat -f %m "$HEARTBEAT_FILE" 2>/dev/null || echo 0)
    NOW=$(date +%s)
    AGE=$(( NOW - HEARTBEAT_MTIME ))
    if [[ "$AGE" -gt "$STALE_SECONDS" ]]; then
        HEALTHY=false
        REASONS+=("Heartbeat is stale (${AGE}s old, threshold=${STALE_SECONDS}s)")
    fi
else
    HEALTHY=false
    REASONS+=("Heartbeat file not found at $HEARTBEAT_FILE")
fi

if [[ "$HEALTHY" == true ]]; then
    echo "reminder-daemon: healthy (pid=$DAEMON_PID, heartbeat ${AGE}s ago)"
    exit 0
fi

# Report unhealthy status
echo "reminder-daemon: UNHEALTHY" >&2
for reason in "${REASONS[@]}"; do
    echo "  - $reason" >&2
done

if [[ "$DO_RESTART" == true ]]; then
    echo "Attempting restart..." >&2
    # Clean up stale files before restarting
    rm -f "$PID_FILE" "$HEARTBEAT_FILE"
    exec "$SCRIPT_DIR/reminder-daemon.sh"
fi

exit 1
