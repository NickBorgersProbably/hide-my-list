#!/usr/bin/env bash
# Record user feedback for a generated reward image.
# Usage: ./scripts/log-reward-feedback.sh <reward_id|latest> <positive|neutral|negative> [note...]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ARCHIVE_DIR="$SCRIPT_DIR/../rewards"
MANIFEST_JSONL="$ARCHIVE_DIR/manifest.jsonl"
FEEDBACK_JSONL="$ARCHIVE_DIR/feedback.jsonl"

TARGET="${1:-}"
REACTION="${2:-}"

usage() {
    cat <<'EOF' >&2
Usage: ./scripts/log-reward-feedback.sh <reward_id|latest> <positive|neutral|negative> [note...]
EOF
    exit 1
}

[ -n "$TARGET" ] || usage
[ -n "$REACTION" ] || usage

case "$REACTION" in
    positive|neutral|negative)
        ;;
    *)
        echo "Unsupported reaction: $REACTION" >&2
        usage
        ;;
esac

shift 2

NOTE="$*"

if [ ! -f "$MANIFEST_JSONL" ]; then
    echo "No reward manifest found at $MANIFEST_JSONL" >&2
    exit 1
fi

FEEDBACK_ENTRY="$(
    TARGET="$TARGET" \
    REACTION="$REACTION" \
    NOTE="$NOTE" \
    MANIFEST_JSONL="$MANIFEST_JSONL" \
    python3 <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

manifest_path = Path(os.environ["MANIFEST_JSONL"])
target = os.environ["TARGET"]
reaction = os.environ["REACTION"]
note = os.environ["NOTE"].strip()

entries = []
for line in manifest_path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        entries.append(json.loads(line))
    except json.JSONDecodeError:
        continue

if not entries:
    print("Reward manifest is empty", file=sys.stderr)
    raise SystemExit(1)

if target == "latest":
    selected = entries[-1]
else:
    selected = next((entry for entry in entries if entry.get("reward_id") == target), None)

if not selected:
    print(f"Reward not found: {target}", file=sys.stderr)
    raise SystemExit(1)

payload = {
    "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "reward_id": selected.get("reward_id"),
    "reaction": reaction,
    "note": note or None,
    "theme_family": selected.get("theme_family"),
    "theme_tags": selected.get("theme_tags", []),
    "style": selected.get("style"),
    "palette": selected.get("palette"),
    "task_mode": selected.get("task_mode"),
    "task_profile": selected.get("task_profile"),
}

print(json.dumps(payload, ensure_ascii=True))
PY
)"

mkdir -p "$ARCHIVE_DIR"
printf '%s\n' "$FEEDBACK_ENTRY" >> "$FEEDBACK_JSONL"

RECORDED_REWARD_ID="$(printf '%s' "$FEEDBACK_ENTRY" | python3 -c 'import json, sys; print(json.load(sys.stdin)["reward_id"])')"
echo "Recorded $REACTION feedback for reward $RECORDED_REWARD_ID"
