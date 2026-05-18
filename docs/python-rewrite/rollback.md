# Cutover Rollback Runbook

This runbook documents the pre-cutover snapshot procedure, the criteria that
trigger a rollback, and the exact commands to revert to OpenClaw if the
Python/LangGraph stack fails during cutover.

**Audience:** the operator performing the Phase D cutover. Read this entirely
before starting any cutover steps.

**Private data discipline:** this document uses placeholder values
(`<repo_root>`, `<peer>`, `YYYYMMDD`) wherever real values depend on the
operator's environment. Never commit real phone numbers, Notion page IDs,
task titles, or personal event details to this file.

---

## 1. Pre-Cutover Snapshot Procedure

Run these steps BEFORE starting Phase D. They create recovery points that allow
a full rollback within ~5 minutes.

### 1.1 Tag the main branch

```bash
git tag pre-cutover-$(date -u +%Y-%m-%d) HEAD
git push origin pre-cutover-$(date -u +%Y-%m-%d)
```

This makes the exact state of main before cutover identifiable.

### 1.2 Postgres dump

```bash
cd <repo_root>
bash docker/backup.sh --backup-dir ./backups
```

Verify the output file exists and is non-empty:

```bash
ls -lh backups/postgres-*.sql.gz | tail -1
```

Expected: a `.sql.gz` file several kilobytes or larger.

### 1.3 COW-copy of state.json

If OpenClaw's `state.json` exists:

```bash
cp state.json state.json.pre-cutover-$(date -u +%Y-%m-%d)
```

Do NOT move or delete `state.json`. The COW-copy preserves it if cutover fails.

### 1.4 COW-copy of signal-cli volume

The signal-cli registration data must survive if the cutover fails. Copy the
volume data to a host directory (do not move it):

```bash
# Find the signal-cli volume mount point:
docker inspect hide-my-list-signal-cli-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
# Typical result: /var/lib/docker/volumes/hide-my-list_signal-cli-data/_data

# COW-copy to a safe location:
sudo cp -a /var/lib/docker/volumes/hide-my-list_signal-cli-data/_data \
  /var/lib/docker/volumes/hide-my-list_signal-cli-data/_data.pre-cutover-$(date -u +%Y-%m-%d)
```

**Do NOT move or delete the original.** If cutover fails, the original volume
mount is still intact and OpenClaw can resume immediately.

### 1.5 Verify backups are restorable (restore drill)

Before the real cutover, verify the Postgres backup can be restored in a
sandboxed environment:

```bash
# Start a throwaway Postgres container with the same user/db shape as the compose stack:
docker run -d --name pg-restore-test \
  -e POSTGRES_USER=hml \
  -e POSTGRES_PASSWORD=hml \
  -e POSTGRES_DB=hml \
  -p 5433:5432 \
  postgres:16-alpine

# Wait for it to be ready:
until docker exec pg-restore-test pg_isready -U hml -d hml; do sleep 1; done

# Restore the most recent backup:
BACKUP=$(ls -t backups/postgres-*.sql.gz | head -1)
gunzip -c "$BACKUP" | docker exec -i pg-restore-test psql -U hml -d hml

# Verify key tables exist:
docker exec pg-restore-test psql -U hml -d hml -c "\dt"

# Tear down:
docker rm -f pg-restore-test
```

If this drill passes, the backup is confirmed restorable.

---

## 2. Signal Registration Carry-Over

The signal-cli container in the new Python stack reads from a Docker volume
that must be pre-populated with the existing Signal registration.

**Method: COW-copy, not move.** (See step 1.4 above.)

When Phase D instructs you to mount the signal-cli volume into the new
compose stack, point the new volume at the COPY, not the original:

```yaml
# docker/compose.yaml (Phase D will update this)
services:
  signal-cli:
    volumes:
      - signal-cli-data:/home/.local/share/signal-cli

volumes:
  signal-cli-data:
    driver_opts:
      type: none
      o: bind
      device: /var/lib/docker/volumes/hide-my-list_signal-cli-data/_data.pre-cutover-<DATE>
```

If cutover fails, the original volume is untouched and OpenClaw resumes.

---

## 3. MEMORY.md / memory/ Daily Files

`MEMORY.md` and `memory/YYYY-MM-DD.md` files are NOT migrated to the new stack.
The Python/LangGraph stack uses the LangGraph Postgres checkpointer for
conversation history; it does not read the memory/ directory.

The operator can keep these files for reference. They do not affect the new
stack's behavior. Phase D does not delete them.

---

## 4. Revert Criteria

Trigger a rollback if ANY of these conditions are observed within 10 minutes
of flipping `ENABLE_LANGGRAPH_PATH=true`:

| Condition | Threshold |
|-----------|-----------|
| Signal messages not delivering | >10 minutes of silence after test send |
| Notion auth error in logs | Any `401` or `403` from `api.notion.com` |
| App crash loop | Container restarting more than 3 times in 5 minutes |
| Database connection failure | Repeated `DATABASE_URL` connection errors in logs |
| Ops alert for `notion_health_failed` with no recovery | Alert arrives + Notion still unreachable |

When in doubt, roll back. Recovery from a rollback is fast; recovery from
data loss is not.

---

## 5. Revert Commands

If rollback is triggered:

### 5.1 Stop the new stack

```bash
cd <repo_root>
docker compose -f docker/compose.yaml down
```

### 5.2 Restore signal-cli volume from COW-copy

If the signal-cli volume data was modified during the new stack run:

```bash
# Overwrite with the pre-cutover COW-copy:
sudo rsync -a --delete \
  /var/lib/docker/volumes/hide-my-list_signal-cli-data/_data.pre-cutover-YYYYMMDD/ \
  /var/lib/docker/volumes/hide-my-list_signal-cli-data/_data/
```

### 5.3 Restart OpenClaw

Start the OpenClaw daemon using whatever mechanism was running it before
(systemd unit, tmux session, etc.):

```bash
# Example: if OpenClaw ran as a systemd user service:
systemctl --user start openclaw

# Or if run manually:
openclaw start
```

### 5.4 Verify recovery

Send a test Signal message and confirm OpenClaw responds. Check that:
- The reminder check cron is registered (`openclaw cron list`)
- state.json is intact (`cat state.json | python3 -c "import json,sys; print(json.load(sys.stdin).keys())"`)

### 5.5 Optional: restore Postgres from backup

If the Postgres data was corrupted or needs to be reset:

```bash
# Stop any containers using Postgres:
docker compose -f docker/compose.yaml down

# Start a fresh Postgres with the same user/db shape as the compose stack:
docker run -d --name pg-restore \
  -e POSTGRES_USER=hml \
  -e POSTGRES_PASSWORD=hml \
  -e POSTGRES_DB=hml \
  -p 5432:5432 \
  postgres:16-alpine

until docker exec pg-restore pg_isready -U hml -d hml; do sleep 1; done

# Restore into the hml database:
BACKUP=$(ls -t backups/postgres-*.sql.gz | head -1)
gunzip -c "$BACKUP" | docker exec -i pg-restore psql -U hml -d hml

docker stop pg-restore
docker rm pg-restore
```

---

## 6. Post-Rollback

After confirming OpenClaw is healthy:

1. File a GitHub issue describing what failed and at what step.
2. Keep the `pre-cutover-YYYYMMDD` tag in git for reference.
3. Do NOT delete `state.json.pre-cutover-YYYYMMDD` until the next cutover attempt.
4. Coordinate with the Phase D subagent / orchestrator before reattempting.
