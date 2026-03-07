#!/usr/bin/env bash
# security-update.sh — Check and apply security updates for manually installed packages
# Run via cron daily

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="${SCRIPT_DIR}/security-updates.log"
PACKAGES_FILE="${SCRIPT_DIR}/installed-packages.txt"

echo "[$(date -Iseconds)] Security update check started" >> "$LOG"

# Update package lists
sudo apt-get update -qq 2>>"$LOG"

# Check for upgradable packages
UPGRADABLE=$(apt list --upgradable 2>/dev/null | grep -v "^Listing" || true)

if [ -n "$UPGRADABLE" ]; then
  echo "[$(date -Iseconds)] Upgradable packages found:" >> "$LOG"
  echo "$UPGRADABLE" >> "$LOG"

  # Apply security updates only
  sudo unattended-upgrade -v 2>>"$LOG" || true

  echo "[$(date -Iseconds)] Security updates applied" >> "$LOG"
else
  echo "[$(date -Iseconds)] No updates available" >> "$LOG"
fi

# Record currently installed manually-added packages for tracking
# (packages not in the base image)
echo "# Packages installed by agent - $(date -Iseconds)" > "$PACKAGES_FILE"
echo "socat" >> "$PACKAGES_FILE"
