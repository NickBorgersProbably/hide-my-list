#!/usr/bin/env bash
# check-reminders.sh — Poll Notion for due reminders and write a signal file
#
# This script is the scheduled reminder system for hide-my-list. It queries
# Notion for reminder tasks whose Remind At time has arrived and writes their
# details to a signal file for the agent to pick up.
#
# The script does NOT update reminder status in Notion — the agent marks
# reminders as sent or missed, and marks the task Completed, after confirmed
# delivery. A successful Notion query is treated as the source of truth for
# which reminders still need delivery, so stale signal entries are cleared
# automatically once the agent updates Notion.
#
# Designed to run from the durable reminder-check cron job (15-minute cadence).
# The script queries Notion for reminder tasks whose Remind At time has arrived
# and writes their details to a repo-root handoff file (default:
# `.reminder-signal`, overridable via `REMINDER_SIGNAL_FILE` in `.env`). This
# script is query-only and does NOT deliver reminders or update Notion statuses
# itself.
#
# Reminder delivery is handled by the delivering agent sessions:
#   - Opportunistic main-session startup check (AGENTS.md step 5): when a user
#     interacts, the main session checks for the handoff file and delivers any
#     pending reminders immediately.
#   - HEARTBEAT.md Check 1 (every 60 minutes): the heartbeat session reads
#     stranded handoff files and delivers reminders as the idle-session backstop.
#
# The delivering session (heartbeat or main session) is responsible for:
#   - Reading the handoff file payload and delivering each reminder to the user
#   - Running `scripts/notion-cli.sh complete-reminder PAGE_ID sent|missed` to
#     atomically set Notion `Status` and `Reminder Status` after confirmed
#     delivery
#   - Deleting the handoff file only after successful delivery and Notion
#     updates (if delivery fails, leave the handoff file in place for retry)
#
# SECURITY PROPERTIES:
#   - Loads only REMINDER_SIGNAL_FILE into this shell; Notion creds stay scoped
#     to notion-cli.sh
#   - REMINDER_SIGNAL_FILE may override only the repo-root handoff filename
#   - Signal file contains only task IDs and titles — no secrets
#   - Missed reminders (>15 min past due) are flagged but still delivered

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/load-env.sh" REMINDER_SIGNAL_FILE?

SIGNAL_BASENAME="${REMINDER_SIGNAL_FILE:-.reminder-signal}"
case "$SIGNAL_BASENAME" in
    ""|"."|".."|*/*)
        echo "check-reminders: REMINDER_SIGNAL_FILE must be a filename in the repo root" >&2
        exit 1
        ;;
esac

SIGNAL_FILE="$ROOT_DIR/$SIGNAL_BASENAME"
NOW_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)
NOW_EPOCH=$(date +%s)
# 15 minutes in seconds — reminders older than this are flagged as missed
MISSED_THRESHOLD=900

echo "check-reminders: checking at $NOW_ISO"

# Query Notion for due reminders
MAX_RETRIES=1
RETRY_DELAY=2
RESPONSE=""
for attempt in $(seq 0 "$MAX_RETRIES"); do
    if RESPONSE=$("$SCRIPT_DIR/notion-cli.sh" query-due-reminders "$NOW_ISO"); then
        break
    fi
    if [ "$attempt" -lt "$MAX_RETRIES" ]; then
        echo "check-reminders: Notion query failed (attempt $((attempt + 1))), retrying in ${RETRY_DELAY}s..." >&2
        sleep "$RETRY_DELAY"
    else
        echo "check-reminders: failed to query Notion after $((MAX_RETRIES + 1)) attempts" >&2
        exit 1
    fi
done

# Parse and validate the response before deciding that nothing is due
if ! RESULT_COUNT=$(echo "$RESPONSE" | python3 -c "
import json, sys

try:
    data = json.load(sys.stdin)
except json.JSONDecodeError as exc:
    print(f'invalid JSON from Notion: {exc}', file=sys.stderr)
    raise SystemExit(1)

if data.get('object') == 'error':
    code = data.get('code', 'unknown_error')
    message = data.get('message', 'unknown Notion API error')
    print(f'Notion API error: {code}: {message}', file=sys.stderr)
    raise SystemExit(1)

results = data.get('results')
if not isinstance(results, list):
    print('Notion response missing results array', file=sys.stderr)
    raise SystemExit(1)

print(len(results))
"); then
    echo "check-reminders: failed to parse Notion response" >&2
    exit 1
fi

if [ "$RESULT_COUNT" -eq 0 ]; then
    rm -f "$SIGNAL_FILE"
    echo "check-reminders: no due reminders"
    exit 0
fi

echo "check-reminders: found $RESULT_COUNT due reminder(s)"

# Process each due reminder and replace the signal file with the current due set
echo "$RESPONSE" | python3 -c "
import json, sys, os, tempfile
from datetime import datetime, timezone

data = json.load(sys.stdin)
results = data.get('results', [])
now_epoch = int(sys.argv[1])
missed_threshold = int(sys.argv[2])
signal_file = sys.argv[3]

new_entries = []
for task in results:
    page_id = task['id']
    props = task.get('properties', {})

    # Extract title
    title_prop = props.get('Title', {}).get('title', [])
    title = title_prop[0]['text']['content'] if title_prop else '(untitled)'

    # Extract remind_at
    remind_at_prop = props.get('Remind At', {}).get('date') or {}
    remind_at_str = remind_at_prop.get('start', '')

    # Determine if missed (>15 min past due)
    status = 'sent'
    if remind_at_str:
        try:
            remind_at_str_clean = remind_at_str.replace('Z', '+00:00')
            remind_dt = datetime.fromisoformat(remind_at_str_clean)
            remind_epoch = int(remind_dt.timestamp())
            if (now_epoch - remind_epoch) > missed_threshold:
                status = 'missed'
        except (ValueError, OSError) as exc:
            print(f'check-reminders: warning: could not parse Remind At '
                  f'{remind_at_str!r} for {page_id}: {exc}', file=sys.stderr)

    new_entries.append({
        'page_id': page_id,
        'title': title,
        'remind_at': remind_at_str,
        'status': status,
    })

payload = json.dumps({
    'checked_at': datetime.now(timezone.utc).isoformat(),
    'reminders': new_entries,
}, indent=2)

signal_dir = os.path.dirname(signal_file) or '.'
fd, tmp_path = tempfile.mkstemp(dir=signal_dir, prefix='.reminder-signal-', suffix='.tmp')
try:
    os.write(fd, payload.encode())
    os.fsync(fd)
    os.close(fd)
    fd = None
    os.rename(tmp_path, signal_file)
except BaseException:
    if fd is not None:
        os.close(fd)
    if os.path.exists(tmp_path):
        os.unlink(tmp_path)
    raise

added = len(new_entries)
print(f'check-reminders: wrote {added} reminder(s) to signal file')

for e in new_entries:
    flag = ' [MISSED]' if e['status'] == 'missed' else ''
    print(f\"  - {e['title']} (due: {e['remind_at']}){flag}\")
" "$NOW_EPOCH" "$MISSED_THRESHOLD" "$SIGNAL_FILE"

echo "check-reminders: done"
