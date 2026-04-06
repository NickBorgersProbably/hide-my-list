#!/usr/bin/env bash
# check-reminders.sh — Poll Notion for due reminders and print a delivery payload
#
# This script is the scheduled reminder query helper for hide-my-list. It asks
# Notion for reminder tasks whose Remind At time has arrived and prints a JSON
# payload describing what is due right now.
#
# It does NOT mark reminders delivered. The isolated `reminder-check` cron turn
# reads this payload, decides whether to reply with NO_REPLY or a user-facing
# reminder message, and then runs `scripts/notion-cli.sh complete-reminder
# PAGE_ID sent|missed` for each reminder it actually delivers.
#
# This keeps Notion as the source of truth for pending reminders while removing
# the old repo-local intermediate file and the multi-path delivery logic. The
# cron session now owns the full check -> announce -> complete flow.
#
# SECURITY PROPERTIES:
#   - Loads no secrets into this shell; Notion credentials stay scoped to
#     notion-cli.sh
#   - Prints only reminder metadata derived from Notion records created by the
#     agent itself
#   - Missed reminders (>15 min past due) are flagged but still returned

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NOW_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)
NOW_EPOCH=$(date +%s)
# 15 minutes in seconds — reminders older than this are flagged as missed
MISSED_THRESHOLD=900

echo "check-reminders: checking at $NOW_ISO" >&2

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

if ! PAYLOAD=$(RESPONSE_JSON="$RESPONSE" python3 - "$NOW_EPOCH" "$MISSED_THRESHOLD" <<'PYTHON'
import json
import os
import sys
from datetime import datetime, timezone

try:
    data = json.loads(os.environ["RESPONSE_JSON"])
except json.JSONDecodeError as exc:
    print(f"invalid JSON from Notion: {exc}", file=sys.stderr)
    raise SystemExit(1)

if data.get("object") == "error":
    code = data.get("code", "unknown_error")
    message = data.get("message", "unknown Notion API error")
    print(f"Notion API error: {code}: {message}", file=sys.stderr)
    raise SystemExit(1)

results = data.get("results")
if not isinstance(results, list):
    print("Notion response missing results array", file=sys.stderr)
    raise SystemExit(1)

now_epoch = int(sys.argv[1])
missed_threshold = int(sys.argv[2])

def title_from_prop(title_prop):
    if not isinstance(title_prop, list) or not title_prop:
        return "(untitled)"
    parts = []
    for chunk in title_prop:
        if not isinstance(chunk, dict):
            continue
        plain_text = chunk.get("plain_text")
        if plain_text:
            parts.append(plain_text)
            continue
        text = chunk.get("text") or {}
        content = text.get("content")
        if content:
            parts.append(content)
    return "".join(parts) or "(untitled)"

reminders = []
for task in results:
    page_id = task["id"]
    props = task.get("properties", {})
    title = title_from_prop(props.get("Title", {}).get("title", []))
    remind_at_prop = props.get("Remind At", {}).get("date") or {}
    remind_at = remind_at_prop.get("start", "")

    status = "sent"
    if remind_at:
        try:
            remind_dt = datetime.fromisoformat(remind_at.replace("Z", "+00:00"))
            remind_epoch = int(remind_dt.timestamp())
            if (now_epoch - remind_epoch) > missed_threshold:
                status = "missed"
        except (ValueError, OSError) as exc:
            print(
                f"check-reminders: warning: could not parse Remind At {remind_at!r} "
                f"for {page_id}: {exc}",
                file=sys.stderr,
            )

    reminders.append(
        {
            "page_id": page_id,
            "title": title,
            "remind_at": remind_at,
            "status": status,
        }
    )

print(
    json.dumps(
        {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "reminders": reminders,
        },
        indent=2,
    )
)
PYTHON
); then
    echo "check-reminders: failed to parse Notion response" >&2
    exit 1
fi

RESULT_COUNT=$(printf '%s' "$PAYLOAD" | python3 -c 'import json, sys; print(len(json.load(sys.stdin)["reminders"]))')

if [ "$RESULT_COUNT" -eq 0 ]; then
    echo "check-reminders: no due reminders" >&2
else
    echo "check-reminders: found $RESULT_COUNT due reminder(s)" >&2
fi

printf '%s\n' "$PAYLOAD"
