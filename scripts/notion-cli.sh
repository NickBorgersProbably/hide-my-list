#!/usr/bin/env bash
# Notion CLI helper for hide-my-list
# Usage: ./notion-cli.sh <command> [args...]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091  # sourced from repo-local scripts dir
source "$SCRIPT_DIR/load-env.sh" NOTION_API_KEY NOTION_DATABASE_ID

API="https://api.notion.com/v1"
HEADERS=(
  -H "Authorization: Bearer $NOTION_API_KEY"
  -H "Notion-Version: 2022-06-28"
  -H "Content-Type: application/json"
)
CURL_ARGS=(-fsS --connect-timeout 10 --max-time 30)

case "${1:-help}" in
  create-task)
    # Args: title work_type urgency time_estimate energy_required [inline_steps] [status]
    TITLE="$2"
    WORK_TYPE="$3"
    URGENCY="${4:-50}"
    TIME_EST="${5:-30}"
    ENERGY="${6:-Medium}"
    INLINE_STEPS="${7:-}"
    STATUS="${8:-Pending}"
    PARENT_ID="${9:-}"
    SEQUENCE="${10:-}"

    PROPS=$(python3 -c "
import json, sys
props = {
    'Title': {'title': [{'text': {'content': sys.argv[1]}}]},
    'Status': {'select': {'name': sys.argv[2]}},
    'Work Type': {'select': {'name': sys.argv[3]}},
    'Urgency': {'number': int(sys.argv[4])},
    'Time Estimate (min)': {'number': int(sys.argv[5])},
    'Energy Required': {'select': {'name': sys.argv[6]}},
    'Rejection Count': {'number': 0},
    'Steps Completed': {'number': 0},
    'Resume Count': {'number': 0},
}
if sys.argv[7]:
    props['Inline Steps'] = {'rich_text': [{'text': {'content': sys.argv[7]}}]}
if sys.argv[8]:
    props['Parent Task'] = {'relation': [{'id': sys.argv[8]}]}
if sys.argv[9]:
    props['Sequence'] = {'number': int(sys.argv[9])}
print(json.dumps({'parent': {'database_id': sys.argv[10]}, 'properties': props}))
" "$TITLE" "$STATUS" "$WORK_TYPE" "$URGENCY" "$TIME_EST" "$ENERGY" "$INLINE_STEPS" "$PARENT_ID" "$SEQUENCE" "$NOTION_DATABASE_ID")

    curl "${CURL_ARGS[@]}" -X POST "$API/pages" "${HEADERS[@]}" -d "$PROPS"
    ;;

  create-reminder)
    # Args: title remind_at_iso [work_type] [energy]
    # Example: notion-cli.sh create-reminder "Email Melanie" "2026-03-15T18:00:00-06:00" "Social" "Low"
    R_TITLE="$2"
    R_REMIND_AT="$3"
    R_WORK_TYPE="${4:-Independent}"
    R_ENERGY="${5:-Low}"

    R_PROPS=$(python3 -c "
import json, sys
props = {
    'Title': {'title': [{'text': {'content': sys.argv[1]}}]},
    'Status': {'select': {'name': 'Pending'}},
    'Work Type': {'select': {'name': sys.argv[2]}},
    'Urgency': {'number': 90},
    'Time Estimate (min)': {'number': 5},
    'Energy Required': {'select': {'name': sys.argv[3]}},
    'Rejection Count': {'number': 0},
    'Steps Completed': {'number': 0},
    'Resume Count': {'number': 0},
    'Is Reminder': {'checkbox': True},
    'Remind At': {'date': {'start': sys.argv[4]}},
    'Reminder Status': {'select': {'name': 'pending'}},
}
print(json.dumps({'parent': {'database_id': sys.argv[5]}, 'properties': props}))
" "$R_TITLE" "$R_WORK_TYPE" "$R_ENERGY" "$R_REMIND_AT" "$NOTION_DATABASE_ID")

    curl "${CURL_ARGS[@]}" -X POST "$API/pages" "${HEADERS[@]}" -d "$R_PROPS"
    ;;

  query-pending)
    curl "${CURL_ARGS[@]}" -X POST "$API/databases/$NOTION_DATABASE_ID/query" "${HEADERS[@]}" \
      -d '{
        "filter": {
          "and": [
            {"property": "Status", "select": {"equals": "Pending"}},
            {"property": "Is Reminder", "checkbox": {"equals": false}}
          ]
        },
        "sorts": [{"property": "Urgency", "direction": "descending"}]
      }'
    ;;

  query-all)
    curl "${CURL_ARGS[@]}" -X POST "$API/databases/$NOTION_DATABASE_ID/query" "${HEADERS[@]}" \
      -d '{"sorts": [{"property": "Urgency", "direction": "descending"}]}'
    ;;

  query-due-reminders)
    # Args: before_iso (ISO 8601 timestamp — return reminders due on or before this time)
    BEFORE_ISO="${2:-$(date -u +%Y-%m-%dT%H:%M:%S%z)}"
    FILTER=$(python3 -c "
import json, sys
print(json.dumps({
    'filter': {
        'and': [
            {'property': 'Is Reminder', 'checkbox': {'equals': True}},
            {'property': 'Reminder Status', 'select': {'equals': 'pending'}},
            {'property': 'Status', 'select': {'equals': 'Pending'}},
            {'property': 'Remind At', 'date': {'on_or_before': sys.argv[1]}}
        ]
    },
    'sorts': [{'property': 'Remind At', 'direction': 'ascending'}]
}))
" "$BEFORE_ISO")
    curl "${CURL_ARGS[@]}" -X POST "$API/databases/$NOTION_DATABASE_ID/query" "${HEADERS[@]}" \
      -d "$FILTER"
    ;;

  update-status)
    # Args: page_id new_status
    PAGE_ID="$2"
    NEW_STATUS="$3"
    # shellcheck disable=SC2034  # retained for backward compatibility
    EXTRA="${4:-}"

    PAGE_JSON=$(curl "${CURL_ARGS[@]}" "$API/pages/$PAGE_ID" "${HEADERS[@]}")

    PROPS=$(python3 - "$NEW_STATUS" "$PAGE_JSON" <<'PYTHON'
import json
import sys
from datetime import datetime, timezone

new_status = sys.argv[1]
page = json.loads(sys.argv[2])

props = {'Status': {'select': {'name': new_status}}}
now = datetime.now(timezone.utc).isoformat()

if new_status == 'Completed':
    props['Completed At'] = {'date': {'start': now}}
elif new_status == 'In Progress':
    started_at = None
    started_prop = page.get('properties', {}).get('Started At')
    if isinstance(started_prop, dict):
        date_value = started_prop.get('date')
        if isinstance(date_value, dict):
            started_at = date_value.get('start')
    if not started_at:
        props['Started At'] = {'date': {'start': now}}

print(json.dumps({'properties': props}))
PYTHON
)

    curl "${CURL_ARGS[@]}" -X PATCH "$API/pages/$PAGE_ID" "${HEADERS[@]}" -d "$PROPS"
    ;;

  complete-reminder)
    # Args: page_id reminder_status
    PAGE_ID="$2"
    REMINDER_STATUS="$3"

    case "$REMINDER_STATUS" in
      sent|missed) ;;
      *)
        echo "Reminder status must be 'sent' or 'missed'" >&2
        exit 1
        ;;
    esac

    PROPS=$(python3 - "$REMINDER_STATUS" <<'PYTHON'
import json
import sys
from datetime import datetime, timezone

reminder_status = sys.argv[1]
now = datetime.now(timezone.utc).isoformat()

print(json.dumps({
    'properties': {
        'Status': {'select': {'name': 'Completed'}},
        'Reminder Status': {'select': {'name': reminder_status}},
        'Completed At': {'date': {'start': now}},
    }
}))
PYTHON
)

    curl "${CURL_ARGS[@]}" -X PATCH "$API/pages/$PAGE_ID" "${HEADERS[@]}" -d "$PROPS"
    ;;

  update-property)
    # Args: page_id property_json
    PAGE_ID="$2"
    PROP_JSON="$3"

    curl "${CURL_ARGS[@]}" -X PATCH "$API/pages/$PAGE_ID" "${HEADERS[@]}" -d "$PROP_JSON"
    ;;

  get-page)
    PAGE_ID="$2"
    curl "${CURL_ARGS[@]}" "$API/pages/$PAGE_ID" "${HEADERS[@]}"
    ;;

  help)
    echo "Usage: notion-cli.sh <command>"
    echo "Commands: create-task, create-reminder, query-pending, query-all, query-due-reminders, update-status, complete-reminder, update-property, get-page"
    ;;
esac
