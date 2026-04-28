#!/usr/bin/env bash
# block-reminder-reasoning-leak.sh
#
# Claude Code Stop hook guard for reminder confirmations. This validates the
# actual final assistant reply and blocks stop when a reminder confirmation
# includes leaked internal commentary such as "Note:" or scheduling internals.

set -euo pipefail

HOOK_INPUT="$(cat)"

HOOK_INPUT="$HOOK_INPUT" python3 - <<'PY'
import json
import os
import pathlib
import re
import sys
import tempfile


CONFIRMATION_PATTERN = re.compile(
    r"\bi.?ll\s+(?:remind|ping|nudge)\s+you\b",
    re.IGNORECASE,
)
LEAK_PATTERN = re.compile(
    r"(^\s*note:)"
    r"|"
    r"\b("
    r"i did not"
    r"|this will not trigger"
    r"|trigger automatically"
    r"|this turn"
    r"|cron"
    r"|poll(?:ing)?"
    r"|handoff"
    r"|tool call"
    r"|hidden reasoning"
    r"|scheduling internals?"
    r")\b",
    re.IGNORECASE | re.MULTILINE,
)
MAX_ATTEMPTS = 2


def attempt_file(session_id: str) -> pathlib.Path:
    state_dir = pathlib.Path(tempfile.gettempdir()) / "hide-my-list-stop-hook"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / f"{session_id}.reminder"


def read_attempt_count(path: pathlib.Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def clear_attempt_count(path: pathlib.Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def main() -> int:
    data = json.loads(os.environ["HOOK_INPUT"])
    message = (data.get("last_assistant_message") or "").strip()
    session_id = data.get("session_id") or "unknown-session"
    state_path = attempt_file(session_id)

    if not message:
        clear_attempt_count(state_path)
        return 0

    if not CONFIRMATION_PATTERN.search(message) or not LEAK_PATTERN.search(message):
        clear_attempt_count(state_path)
        return 0

    attempts = read_attempt_count(state_path)
    if attempts >= MAX_ATTEMPTS:
        clear_attempt_count(state_path)
        return 0

    state_path.write_text(str(attempts + 1), encoding="utf-8")
    json.dump(
        {
            "decision": "block",
            "reason": (
                "Your last reply mixed a reminder confirmation with internal "
                "scheduling commentary. Rewrite it as exactly one brief "
                "user-facing reminder confirmation sentence. Remove any note "
                "or explanation about tool use, cron, polling, delivery "
                "windows, hidden reasoning, trigger behavior, or what "
                "happened 'in this turn'."
            ),
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
PY
