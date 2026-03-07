#!/usr/bin/env bash
# webhook-signal.sh — Minimal webhook receiver for PR review notifications
#
# SECURITY PROPERTIES:
#   - Request content (body, headers, path, query) is NEVER read, logged, or stored
#   - stdin from client socket is redirected to /dev/null before any processing
#   - Signal file contains only a self-generated unix timestamp
#   - socat max-children=2 limits concurrent connections (slowloris mitigation)
#   - timeout(1) hard-kills any connection handler after 3 seconds
#   - No data from the HTTP request ever reaches the LLM context
#
# The ONLY effect of receiving a request: a timestamp file is created.
# The agent periodically checks for this file and, if present, queries
# GitHub's public API for open PR review comments.

set -euo pipefail

SIGNAL_FILE="/home/caroline/.openclaw/workspace/hide-my-list/.pr-signal"
PORT="${WEBHOOK_PORT:-9199}"

echo "webhook-signal: listening on port $PORT (pid $$)"

# Handler script: close stdin immediately (discard request), send response, write signal.
# Written to a temp file so the SYSTEM command is trivially simple.
HANDLER="$(mktemp)"
cat > "$HANDLER" << 'SCRIPT'
exec 0</dev/null
printf 'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK'
date +%s > SIGNAL_FILE_PLACEHOLDER
SCRIPT
sed -i "s|SIGNAL_FILE_PLACEHOLDER|${SIGNAL_FILE}|" "$HANDLER"

trap 'rm -f "$HANDLER"; exit 0' INT TERM EXIT

# socat forks a child per connection. Each child:
#   - runs under timeout(1) — hard kill at 3 seconds
#   - immediately closes stdin (discards all request data)
#   - writes static HTTP 200 to stdout (client socket)
#   - writes timestamp to signal file
#
# max-children=2: concurrent connection limit

exec socat \
  "TCP-LISTEN:${PORT},fork,reuseaddr,max-children=2" \
  "SYSTEM:timeout 3 bash ${HANDLER}" \
  2>/dev/null
