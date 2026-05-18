#!/usr/bin/env bash
# Postgres backup for hide-my-list.
#
# Dumps the running postgres service via pg_dump, compresses the output,
# and writes it to ./backups/postgres-YYYYMMDD-HHMMSS.sql.gz on the host.
# Retains the most recent 30 daily backups; deletes older files.
#
# Scheduling: run this script from the HOST (not inside the container).
# Recommended: operator-controlled cron, systemd timer, or external scheduler.
# Example host crontab entry (adjust path to match your repo location):
#
#   0 4 * * * /path/to/hide-my-list/docker/backup.sh
#
# Do NOT add crontab entries from within this repo (infra agent's concern).
#
# Usage:
#   ./docker/backup.sh [OPTIONS]
#
# Options:
#   --compose-file PATH   Path to docker-compose file (default: docker/compose.yaml
#                         relative to this script's directory)
#   --backup-dir PATH     Directory for backup files (default: ./backups next to
#                         this script's parent directory)
#   --retain N            Number of backups to keep (default: 30)
#   --dry-run             Print what would be done without writing files
#
# Requires: docker compose (v2), pg_dump (or uses postgres container directly)
# Environment (optional):
#   POSTGRES_USER     Postgres username (default: postgres)
#   POSTGRES_DB       Postgres database name (default: postgres)

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

COMPOSE_FILE="${SCRIPT_DIR}/compose.yaml"
BACKUP_DIR="${REPO_ROOT}/backups"
RETAIN=30
DRY_RUN=false

POSTGRES_USER="${POSTGRES_USER:-hml}"
POSTGRES_DB="${POSTGRES_DB:-hml}"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
  case "$1" in
    --compose-file)
      COMPOSE_FILE="$2"
      shift 2
      ;;
    --backup-dir)
      BACKUP_DIR="$2"
      shift 2
      ;;
    --retain)
      RETAIN="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------

if [[ ! -f "${COMPOSE_FILE}" ]]; then
  echo "ERROR: compose file not found: ${COMPOSE_FILE}" >&2
  exit 1
fi

if [[ ! "${RETAIN}" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: --retain must be a positive integer, got: ${RETAIN}" >&2
  exit 1
fi

TIMESTAMP="$(date -u '+%Y%m%d-%H%M%S')"
BACKUP_FILE="${BACKUP_DIR}/postgres-${TIMESTAMP}.sql.gz"
BACKUP_TMP="${BACKUP_DIR}/postgres-${TIMESTAMP}.sql.gz.tmp"

echo "backup.sh: starting Postgres backup"
echo "  Compose file : ${COMPOSE_FILE}"
echo "  Backup dir   : ${BACKUP_DIR}"
echo "  Output file  : ${BACKUP_FILE} (via temp file)"
echo "  Retain       : ${RETAIN} backups"
echo "  Dry run      : ${DRY_RUN}"

if [[ "${DRY_RUN}" == "true" ]]; then
  echo "DRY RUN: would write ${BACKUP_FILE} and prune to ${RETAIN} backups."
  exit 0
fi

# ---------------------------------------------------------------------------
# Create backup directory if needed
# ---------------------------------------------------------------------------

mkdir -p "${BACKUP_DIR}"

# ---------------------------------------------------------------------------
# Run pg_dump via docker compose exec
# ---------------------------------------------------------------------------

# Write to a temp file first; rename to final path only after verification.
# This prevents a corrupt file from appearing as the newest backup if pg_dump fails.
docker compose --file "${COMPOSE_FILE}" exec -T postgres \
  pg_dump --username="${POSTGRES_USER}" --dbname="${POSTGRES_DB}" \
  | gzip > "${BACKUP_TMP}"

# ---------------------------------------------------------------------------
# Verify the dump is non-empty before promoting to final path
# ---------------------------------------------------------------------------

FILESIZE=$(stat -c '%s' "${BACKUP_TMP}" 2>/dev/null || stat -f '%z' "${BACKUP_TMP}" 2>/dev/null || echo 0)
if [[ "${FILESIZE}" -lt 100 ]]; then
  echo "ERROR: backup file appears too small (${FILESIZE} bytes) — may be corrupt." >&2
  rm -f "${BACKUP_TMP}"
  exit 1
fi

mv "${BACKUP_TMP}" "${BACKUP_FILE}"
echo "backup.sh: wrote ${BACKUP_FILE} (${FILESIZE} bytes)"

# ---------------------------------------------------------------------------
# Retention: keep the most recent RETAIN backups; delete older ones
# ---------------------------------------------------------------------------

# List all backup files sorted oldest-first; delete any beyond the retain limit.
BACKUP_FILES=()
while IFS= read -r -d '' f; do
  BACKUP_FILES+=("$f")
done < <(find "${BACKUP_DIR}" -maxdepth 1 -name 'postgres-*.sql.gz' -print0 | sort -z)

TOTAL="${#BACKUP_FILES[@]}"
DELETE_COUNT=$(( TOTAL - RETAIN ))

if [[ "${DELETE_COUNT}" -gt 0 ]]; then
  echo "backup.sh: pruning ${DELETE_COUNT} old backup(s) (keeping ${RETAIN})"
  for (( i=0; i<DELETE_COUNT; i++ )); do
    echo "  deleting: ${BACKUP_FILES[$i]}"
    rm -f "${BACKUP_FILES[$i]}"
  done
else
  echo "backup.sh: no pruning needed (${TOTAL} backup(s) present, retain=${RETAIN})"
fi

echo "backup.sh: done"
