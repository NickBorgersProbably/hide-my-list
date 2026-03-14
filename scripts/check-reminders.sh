#!/usr/bin/env bash
# check-reminders.sh — Poll Notion for due reminders and write a signal file
#
# This script is the scheduled reminder system for hide-my-list. It queries
# Notion for reminder tasks whose Remind At time has arrived and writes their
# details to a signal file for the agent to pick up.
#
# The script does NOT update reminder status in Notion — the agent marks
# reminders as sent/completed after confirmed delivery. This gives
# at-least-once semantics: a duplicate delivery is far better than a lost
# reminder for an ADHD user relying on this system.
#
# If a signal file already exists with unconsumed reminders, new entries are
# merged in (deduped by page_id) rather than overwriting.
#
# Designed to run via reminder-daemon.sh (default: every 5 minutes).
# The agent checks for the signal file and delivers reminders to the user
# through the active messaging surface.
#
# SECURITY PROPERTIES:
#   - Uses the same .env credential loading as notion-cli.sh
#   - Signal file contains only task IDs and titles — no secrets
#   - Missed reminders (>15 min past due) are flagged but still delivered

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT_DIR/.env"

SIGNAL_FILE="${REMINDER_SIGNAL_FILE:-$ROOT_DIR/.reminder-signal}"
NOW_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)
NOW_EPOCH=$(date +%s)
# 15 minutes in seconds — reminders older than this are flagged as missed
MISSED_THRESHOLD=900

echo "check-reminders: checking at $NOW_ISO"

# Query Notion for due reminders
RESPONSE=$("$SCRIPT_DIR/notion-cli.sh" query-due-reminders "$NOW_ISO")

# Parse the response — extract results array length
RESULT_COUNT=$(echo "$RESPONSE" | python3 -c "
import json, sys
data = json.load(sys.stdin)
results = data.get('results', [])
print(len(results))
" 2>/dev/null || echo "0")

if [ "$RESULT_COUNT" -eq 0 ]; then
    echo "check-reminders: no due reminders"
    exit 0
fi

echo "check-reminders: found $RESULT_COUNT due reminder(s)"

# Process each due reminder — merge into signal file (dedup by page_id)
echo "$RESPONSE" | python3 -c "
import json, sys, os
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
    remind_at_prop = props.get('Remind At', {}).get('date', {})
    remind_at_str = remind_at_prop.get('start', '') if remind_at_prop else ''

    # Determine if missed (>15 min past due)
    status = 'sent'
    if remind_at_str:
        try:
            remind_at_str_clean = remind_at_str.replace('Z', '+00:00')
            remind_dt = datetime.fromisoformat(remind_at_str_clean)
            remind_epoch = int(remind_dt.timestamp())
            if (now_epoch - remind_epoch) > missed_threshold:
                status = 'missed'
        except (ValueError, OSError):
            pass

    new_entries.append({
        'page_id': page_id,
        'title': title,
        'remind_at': remind_at_str,
        'status': status,
    })

# Merge with existing signal file to avoid dropping unconsumed reminders
existing_entries = []
if os.path.exists(signal_file):
    try:
        with open(signal_file, 'r') as f:
            existing_data = json.load(f)
            existing_entries = existing_data.get('reminders', [])
    except (json.JSONDecodeError, OSError):
        pass

# Dedup by page_id — new entries take precedence over stale ones
seen_ids = {e['page_id'] for e in new_entries}
merged = list(new_entries)
for existing in existing_entries:
    if existing.get('page_id') not in seen_ids:
        merged.append(existing)

with open(signal_file, 'w') as f:
    f.write(json.dumps({
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'reminders': merged,
    }, indent=2))

added = len(new_entries)
kept = len(merged) - added
print(f'check-reminders: wrote {added} new + {kept} existing reminder(s) to signal file')

for e in new_entries:
    flag = ' [MISSED]' if e['status'] == 'missed' else ''
    print(f\"  - {e['title']} (due: {e['remind_at']}){flag}\")
" "$NOW_EPOCH" "$MISSED_THRESHOLD" "$SIGNAL_FILE"

echo "check-reminders: done"
