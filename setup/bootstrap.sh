#!/usr/bin/env bash
# bootstrap.sh — Set up a new hide-my-list OpenClaw workspace
#
# Run this after cloning the repo into ~/.openclaw/workspace/
# It creates per-user files from templates, verifies prerequisites,
# and registers durable cron jobs.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"

echo "=== hide-my-list bootstrap ==="
echo "Workspace: $ROOT_DIR"
echo "OpenClaw home: $OPENCLAW_HOME"
echo ""

# --- Prerequisites ---

if ! command -v openclaw &>/dev/null; then
    echo "ERROR: openclaw not found in PATH. Install OpenClaw first."
    exit 1
fi

if [ ! -f "$ROOT_DIR/.env" ]; then
    echo "ERROR: .env file not found at $ROOT_DIR/.env"
    echo ""
    echo "Create it with:"
    echo "  NOTION_API_KEY=ntn_..."
    echo "  NOTION_DATABASE_ID=..."
    echo "  SIGNAL_OWNER_NUMBER=+15551234567  (required for cron delivery)"
    echo "  OPENAI_API_KEY=sk-...  (optional, for reward images)"
    echo "  GITHUB_PAT=ghp_...     (optional, for higher API rate limits)"
    exit 1
fi

# Verify required .env vars
# shellcheck source=/dev/null
source "$ROOT_DIR/.env"
if [ -z "${NOTION_API_KEY:-}" ]; then
    echo "ERROR: NOTION_API_KEY not set in .env"
    exit 1
fi
if [ -z "${NOTION_DATABASE_ID:-}" ]; then
    echo "ERROR: NOTION_DATABASE_ID not set in .env"
    exit 1
fi
if [ -z "${SIGNAL_OWNER_NUMBER:-}" ]; then
    echo "ERROR: SIGNAL_OWNER_NUMBER not set in .env"
    exit 1
fi

echo "Prerequisites OK"
echo ""

# --- Per-user files ---

if [ ! -f "$ROOT_DIR/USER.md" ]; then
    if [ -t 0 ]; then
        # Interactive mode — prompt for values
        read -rp "Your name: " USER_NAME
        read -rp "What to call you: " DISPLAY_NAME
        read -rp "Timezone (e.g. US Central): " TIMEZONE
        read -rp "TZ identifier (e.g. America/Chicago): " TZ_ID
        read -rp "Notes about you (optional): " USER_NOTES

        sed \
            -e "s/{{USER_NAME}}/$USER_NAME/" \
            -e "s/{{DISPLAY_NAME}}/$DISPLAY_NAME/" \
            -e "s/{{TIMEZONE}}/$TIMEZONE/" \
            -e "s/{{TZ_IDENTIFIER}}/$TZ_ID/" \
            -e "s/{{USER_NOTES}}/${USER_NOTES:-None yet}/" \
            -e "s/{{ADDITIONAL_CONTEXT}}//" \
            "$ROOT_DIR/USER.md.template" > "$ROOT_DIR/USER.md"
        echo "Created USER.md"
    else
        cp "$ROOT_DIR/USER.md.template" "$ROOT_DIR/USER.md"
        echo "Copied USER.md.template -> USER.md (edit it with your details)"
    fi
else
    echo "USER.md already exists, skipping"
fi

if [ ! -f "$ROOT_DIR/MEMORY.md" ]; then
    # Get timezone from USER.md if it exists, otherwise use placeholder
    TZ_FOR_MEMORY="(set your timezone)"
    if [ -f "$ROOT_DIR/USER.md" ]; then
        TZ_FOR_MEMORY=$(grep -oP 'Timezone:\*\* \K.*' "$ROOT_DIR/USER.md" 2>/dev/null || echo "(set your timezone)")
    fi
    sed "s/{{TIMEZONE}}/$TZ_FOR_MEMORY/" "$ROOT_DIR/MEMORY.md.template" > "$ROOT_DIR/MEMORY.md"
    echo "Created MEMORY.md"
else
    echo "MEMORY.md already exists, skipping"
fi

# --- Directories ---

mkdir -p "$ROOT_DIR/memory"
mkdir -p "$ROOT_DIR/rewards"
echo "Ensured runtime directories exist"
echo ""

# --- Notion connectivity ---

echo "Testing Notion connectivity..."
if "$ROOT_DIR/scripts/notion-cli.sh" query-pending > /dev/null 2>&1; then
    echo "Notion API: OK"
else
    echo "WARNING: Notion API check failed. Verify NOTION_API_KEY and NOTION_DATABASE_ID in .env"
fi
echo ""

# --- OpenClaw config ---

if [ -f "$OPENCLAW_HOME/openclaw.json" ]; then
    echo "openclaw.json already exists at $OPENCLAW_HOME/openclaw.json, skipping"
else
    echo "NOTE: openclaw.json template is at setup/openclaw.json.template"
    echo "Copy it to $OPENCLAW_HOME/openclaw.json and fill in the {{PLACEHOLDER}} values."
fi
echo ""

# --- Cron jobs ---

echo "Cron job registration:"
echo "  After starting the OpenClaw agent, register durable cron jobs."
echo "  See setup/cron/ for job definitions and prompts."
echo ""
echo "  The HEARTBEAT.md will also re-register cron jobs if they expire."
echo ""

# --- Done ---

echo "=== Bootstrap complete ==="
echo ""
echo "Next steps:"
echo "  1. Start OpenClaw: openclaw gateway"
echo "  2. Register cron jobs (see setup/cron/ for definitions)"
echo "  3. Send a test message to verify the agent responds"
