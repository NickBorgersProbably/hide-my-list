#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

require_command() {
  local cmd="$1"

  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: required command '$cmd' is not installed." >&2
    exit 1
  fi
}

require_command openclaw
require_command jq
require_command python3

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

config_path="$tmp_dir/openclaw.json"
state_dir="$tmp_dir/state"
openclaw_home="$tmp_dir/home"
mkdir -p "$state_dir" "$openclaw_home/workspace"

openclaw_version="$(openclaw --version | awk '{print $2}')"

python3 - "$openclaw_home" "$config_path" "$openclaw_version" <<'PY'
import pathlib
import sys

openclaw_home, config_path, openclaw_version = sys.argv[1:]
template = pathlib.Path("setup/openclaw.json.template").read_text(encoding="utf-8")
replacements = {
    "OPENCLAW_VERSION": openclaw_version,
    "SETUP_DATE": "2026-05-03T00:00:00Z",
    "LITELLM_BASE_URL": "http://127.0.0.1:4000",
    "LITELLM_API_KEY": "ci-litellm-key",
    "OPENCLAW_HOME": openclaw_home,
    "TZ_IDENTIFIER": "America/Chicago",
    "SIGNAL_PHONE_NUMBER": "+15555550100",
    "CONTROL_UI_ORIGIN": "http://127.0.0.1:18789",
    "GATEWAY_AUTH_TOKEN": "ci-gateway-token-0000000000000000",
}
rendered = template
for key, value in replacements.items():
    rendered = rendered.replace("{{" + key + "}}", value)

pathlib.Path(config_path).write_text(rendered, encoding="utf-8")
PY

if grep -q '{{[A-Z0-9_]\+}}' "$config_path"; then
  echo "ERROR: rendered OpenClaw config still contains template placeholders" >&2
  grep -o '{{[A-Z0-9_]\+}}' "$config_path" | sort -u >&2
  exit 1
fi

echo "=== Checking default web tools are disabled ==="
jq -e '
  .tools.web.search.enabled == false
  and .tools.web.fetch.enabled == false
' "$config_path" >/dev/null

echo "=== Checking prompt-footprint baseline excludes optional extras ==="
jq -e '
  (has("auth") | not)
  and ((.agents.defaults.model // {}) | has("fallbacks") | not)
  and ((.agents.defaults // {}) | has("maxConcurrent") | not)
  and ((.agents.defaults // {}) | has("subagents") | not)
  and (has("messages") | not)
  and (has("commands") | not)
  and ((.skills // {}) | has("install") | not)
  and ((.channels.signal // {}) | has("defaultTo") | not)
' "$config_path" >/dev/null

export OPENCLAW_CONFIG_PATH="$config_path"
export OPENCLAW_STATE_DIR="$state_dir"

echo "=== Validating rendered OpenClaw config ==="
openclaw config validate --json | jq -e '.valid == true' >/dev/null

echo "=== Reading heartbeat subtree through OpenClaw ==="
heartbeat_json="$(openclaw config get agents.defaults.heartbeat --json)"
template_heartbeat_json="$(jq -c '.agents.defaults.heartbeat' "$config_path")"
jq -e \
  --argjson expected "$template_heartbeat_json" \
  --argjson actual "$heartbeat_json" \
  -n '$actual == $expected' >/dev/null

echo "=== Validating heartbeat subtree write path ==="
openclaw config set agents.defaults.heartbeat "$heartbeat_json" --strict-json --dry-run >/dev/null
openclaw config set agents.defaults.heartbeat "$heartbeat_json" --strict-json >/dev/null

echo "=== Re-validating after OpenClaw config write ==="
openclaw config validate --json | jq -e '.valid == true' >/dev/null

echo "=== Exercising OpenClaw schema output ==="
openclaw config schema | jq -e '.type == "object"' >/dev/null

echo "OpenClaw config smoke passed"
