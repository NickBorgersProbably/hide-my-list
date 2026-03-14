#!/usr/bin/env bash
# check-reminders.sh — Poll Notion for due reminders and write a signal file
#
# This script is the scheduled reminder system for hide-my-list. It queries
# Notion for reminder tasks whose Remind At time has arrived, writes their
# details to a signal file, and marks them as sent.
#
# Designed to run on a cron schedule (e.g., every 5 minutes via GitHub Actions
# or a local cron job). The agent checks for the signal file and delivers
# reminders to the user through the active messaging surface.
#
# SECURITY PROPERTIES:
#   - Uses the same .env credential loading as notion-cli.sh
#   - Signal file contains only task IDs and titles — no secrets
#   - Missed reminders (>15 min past due) are flagged but still delivered

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/../.env"

SIGNAL_FILE="${REMINDER_SIGNAL_FILE:-/home/caroline/.openclaw/workspace/hide-my-list/.reminder-signal}"
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

# Process each due reminder
echo "$RESPONSE" | python3 -c "
import json, sys, os
from datetime import datetime, timezone

data = json.load(sys.stdin)
results = data.get('results', [])
now_epoch = int(sys.argv[1])
missed_threshold = int(sys.argv[2])
signal_file = sys.argv[3]

entries = []
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
            # Parse ISO 8601 timestamp
            remind_at_str_clean = remind_at_str.replace('Z', '+00:00')
            remind_dt = datetime.fromisoformat(remind_at_str_clean)
            remind_epoch = int(remind_dt.timestamp())
            if (now_epoch - remind_epoch) > missed_threshold:
                status = 'missed'
        except (ValueError, OSError):
            pass

    entries.append({
        'page_id': page_id,
        'title': title,
        'remind_at': remind_at_str,
        'status': status,
    })

# Write signal file with reminder details
with open(signal_file, 'w') as f:
    f.write(json.dumps({
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'reminders': entries,
    }, indent=2))

print(f'check-reminders: wrote {len(entries)} reminder(s) to signal file')

# Print summary
for e in entries:
    flag = ' [MISSED]' if e['status'] == 'missed' else ''
    print(f\"  - {e['title']} (due: {e['remind_at']}){flag}\")
" "$NOW_EPOCH" "$MISSED_THRESHOLD" "$SIGNAL_FILE"

# Update each reminder's status in Notion
echo "$RESPONSE" | python3 -c "
import json, sys
from datetime import datetime, timezone

data = json.load(sys.stdin)
results = data.get('results', [])
now_epoch = int(sys.argv[1])
missed_threshold = int(sys.argv[2])

for task in results:
    page_id = task['id']
    props = task.get('properties', {})

    remind_at_prop = props.get('Remind At', {}).get('date', {})
    remind_at_str = remind_at_prop.get('start', '') if remind_at_prop else ''

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

    notion_status = 'Completed' if status == 'sent' else 'Pending'
    print(f'{page_id}\t{status}\t{notion_status}')
" "$NOW_EPOCH" "$MISSED_THRESHOLD" | while IFS=$'\t' read -r PAGE_ID REMINDER_STATUS NOTION_STATUS; do
    # Update Reminder Status property
    "$SCRIPT_DIR/notion-cli.sh" update-property "$PAGE_ID" "$(python3 -c "
import json, sys
print(json.dumps({'properties': {
    'Reminder Status': {'select': {'name': sys.argv[1]}},
    'Status': {'select': {'name': sys.argv[2]}},
}}))
" "$REMINDER_STATUS" "$NOTION_STATUS")" > /dev/null

    echo "check-reminders: updated $PAGE_ID → reminder_status=$REMINDER_STATUS, status=$NOTION_STATUS"
done

echo "check-reminders: done"
