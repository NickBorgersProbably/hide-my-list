#!/usr/bin/env bash
# migrate-openclaw-config.sh — add the Haiku reminder-delivery model to openclaw.json

set -euo pipefail

MODE="${1:-}"
if [ -n "$MODE" ] && [ "$MODE" != "--check" ]; then
    echo "usage: $0 [--check]" >&2
    exit 2
fi

OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
CONFIG_FILE="$OPENCLAW_HOME/openclaw.json"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "openclaw config not found at $CONFIG_FILE" >&2
    exit 1
fi

python3 - "$CONFIG_FILE" "$MODE" <<'PY'
import json
import os
import sys
import tempfile

config_path = sys.argv[1]
mode = sys.argv[2]
model_id = "claude-haiku-4-5"
model_def = {
    "id": model_id,
    "name": "Claude Haiku 4.5",
    "reasoning": False,
    "input": ["text", "image"],
    "contextWindow": 200000,
    "maxTokens": 8192,
}

with open(config_path, "r", encoding="utf-8") as fh:
    data = json.load(fh)

try:
    models = data["models"]["providers"]["litellm"]["models"]
except KeyError as exc:
    print(f"openclaw config is missing expected path for litellm models: {exc}", file=sys.stderr)
    raise SystemExit(1)

if not isinstance(models, list):
    print("openclaw config has a non-list litellm models field", file=sys.stderr)
    raise SystemExit(1)

already_present = any(isinstance(item, dict) and item.get("id") == model_id for item in models)
if already_present:
    print(f"{model_id} already present in {config_path}")
    raise SystemExit(0)

if mode == "--check":
    print(f"{model_id} missing from {config_path}", file=sys.stderr)
    raise SystemExit(1)

models.append(model_def)

directory = os.path.dirname(config_path) or "."
fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".openclaw-", suffix=".tmp")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, config_path)
except BaseException:
    if os.path.exists(tmp_path):
        os.unlink(tmp_path)
    raise

print(f"Added {model_id} to {config_path}")
PY

if [ -z "$MODE" ]; then
    echo "Restart the OpenClaw gateway so it reloads the updated config."
fi
