#!/usr/bin/env bash
# user-time-context.sh — Print user-local calendar context for reminder parsing
#
# Usage:
#   scripts/user-time-context.sh
#   scripts/user-time-context.sh 2026-04-19T01:27:00Z
#
# Reads the user's IANA timezone identifier from USER.md and converts the
# provided reference timestamp (or "now") into both UTC and user-local
# calendar context. This gives reminder intake a concrete source of truth for
# relative phrases like "today", "tomorrow", and "tonight".

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
USER_FILE="$ROOT_DIR/USER.md"
REFERENCE_INPUT="${1:-now}"

if [ ! -f "$USER_FILE" ]; then
    echo "user-time-context: USER.md not found at $USER_FILE" >&2
    exit 1
fi

USER_TZ="$(
    sed -nE 's/^- \*\*Timezone:\*\* .* \(([^()]+)\)$/\1/p' "$USER_FILE" | head -n 1
)"

if [ -z "$USER_TZ" ]; then
    echo "user-time-context: could not extract timezone identifier from $USER_FILE" >&2
    exit 1
fi

USER_TZ="$USER_TZ" REFERENCE_INPUT="$REFERENCE_INPUT" python3 - <<'PY'
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

user_tz_name = os.environ["USER_TZ"]
reference_input = os.environ["REFERENCE_INPUT"].strip()

try:
    user_tz = ZoneInfo(user_tz_name)
except Exception as exc:  # pragma: no cover - shell wrapper handles failure
    print(
        f"user-time-context: invalid timezone identifier {user_tz_name!r}: {exc}",
        file=sys.stderr,
    )
    raise SystemExit(1)

if reference_input == "now":
    reference_utc = datetime.now(timezone.utc)
else:
    normalized = reference_input.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        print(
            f"user-time-context: could not parse timestamp {reference_input!r}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    reference_utc = parsed.astimezone(timezone.utc)

reference_local = reference_utc.astimezone(user_tz)
tomorrow_local_date = reference_local.date() + timedelta(days=1)
tomorrow_local = datetime.combine(
    tomorrow_local_date,
    reference_local.timetz().replace(tzinfo=None),
    tzinfo=user_tz,
)

payload = {
    "user_timezone": user_tz_name,
    "reference_utc": reference_utc.isoformat().replace("+00:00", "Z"),
    "reference_local": reference_local.isoformat(),
    "local_date": reference_local.date().isoformat(),
    "local_day_of_week": reference_local.strftime("%A"),
    "tomorrow_date": tomorrow_local_date.isoformat(),
    "tomorrow_day_of_week": tomorrow_local.strftime("%A"),
}

json.dump(payload, sys.stdout, indent=2)
sys.stdout.write("\n")
PY
