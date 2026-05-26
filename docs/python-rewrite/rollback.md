# Cutover Rollback Runbook

This runbook documents the pre-cutover snapshot procedure, the forward cutover
steps, the criteria that trigger a rollback, and the exact commands to revert
if the Python/LangGraph stack fails.

**Audience:** the operator performing the Phase D cutover. Read this entirely
before starting any cutover steps.

**Private data discipline:** this document uses placeholder values
(`<repo_root>`, `<peer>`, `YYYYMMDD`) wherever real values depend on the
operator's environment. Never commit real phone numbers, Notion page IDs,
task titles, or personal event details to this file.

**What infra owns vs. what this repo owns:**

- **Infra owns:** signal-cli container provisioning + Signal registration
  carry-over; host firewall; deployment topology; VM isolation; the old
  OpenClaw daemon lifecycle (stop/disable post-cutover). signal-cli is
  infrastructure-provided — verify connectivity with
  `docker compose exec signal-cli signal-cli ...` or the REST API healthcheck.
  Volume + registration lifecycle is outside this repo.
- **Repo owns:** `docker/compose.yaml`, app code, migrations, scheduled jobs,
  Postgres backup script.

---

## 1. Pre-Cutover Snapshot

Run these steps BEFORE starting the cutover. They create recovery points that
allow a full rollback within ~5 minutes.

### 1.1 Tag the main branch

```bash
git tag pre-cutover-$(date -u +%Y-%m-%d) HEAD
git push origin pre-cutover-$(date -u +%Y-%m-%d)
```

### 1.2 Postgres backup

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

### 1.4 Verify backup is restorable (restore drill)

Before the real cutover, verify the Postgres backup can be restored:

```bash
# Start a throwaway Postgres container with the same user/db shape as the compose stack:
docker run -d --name pg-restore-test \
  -e POSTGRES_USER=hml \
  -e POSTGRES_PASSWORD=hml \
  -e POSTGRES_DB=hml \
  -p 5433:5432 \
  postgres:16-alpine

until docker exec pg-restore-test pg_isready -U hml -d hml; do sleep 1; done

BACKUP=$(ls -t backups/postgres-*.sql.gz | head -1)
gunzip -c "$BACKUP" | docker exec -i pg-restore-test psql -U hml -d hml

docker exec pg-restore-test psql -U hml -d hml -c "\dt"

docker rm -f pg-restore-test
```

If this drill passes, the backup is confirmed restorable.

---

## 2. Forward Cutover Procedure

### 2.1 Stop the OpenClaw daemon (infra-managed)

Stop and disable the OpenClaw daemon using whatever mechanism was running it
(systemd unit, tmux session, etc.):

```bash
# Example: if OpenClaw ran as a systemd user service:
systemctl --user stop openclaw
systemctl --user disable openclaw

# Or if run manually in tmux:
# (kill the tmux window/session running openclaw)
```

The infra agent or operator controls the exact stop command. The key signal:
signal-cli is no longer delivering new messages to OpenClaw.

### 2.2 Run state migration (one-time, idempotent)

```bash
cd <repo_root>
python scripts/migrate_state_json.py --state-json state.json --peer <E.164_peer>
```

Where `<E.164_peer>` is the inbound Signal peer number (the user's phone number,
NOT `SIGNAL_ACCOUNT`). Can also be set via `SIGNAL_PEER` env var.

This is idempotent — safe to run multiple times. It reads `state.json` and
writes `user_prefs`, reward prefs, and any unresolved `recent_outbound` entries
into Postgres.

### 2.3 Start the Python stack

```bash
cd <repo_root>
docker compose -f docker/compose.yaml up -d
```

`ENABLE_LANGGRAPH_PATH=true` is the default post-Phase-D. The app starts the
Signal ingress listener, APScheduler, and reminder worker.

### 2.4 Cutover smoke test

See Section 4 (Cutover Smoke Test) below. Run it now before declaring success.

---

## 3. MEMORY.md / memory/ Daily Files

`MEMORY.md` and `memory/YYYY-MM-DD.md` files are NOT migrated to the new stack.
The Python/LangGraph stack uses the LangGraph Postgres checkpointer for
conversation history; it does not read the memory/ directory.

The operator can keep these files for reference. They do not affect the new
stack's behavior.

---

## 4. Cutover Smoke Test

Run after `docker compose up -d` in Section 2.3. Operator executes manually.
Not a CI step.

1. **Signal round-trip:** Send "I have 30 minutes" via Signal. Expect a
   GET_TASK task suggestion within 5 seconds. Check `docker compose logs -f app`
   for the graph invocation.

2. **Reminder outbox:** Send "remind me to test in 2 minutes". Query
   `reminder_outbox` for a `pending` row:
   ```bash
   docker exec -it $(docker compose ps -q postgres) \
     psql -U hml -d hml -c "SELECT id, state, due_at FROM reminder_outbox ORDER BY created_at DESC LIMIT 5;"
   ```
   Wait 2 minutes; expect `state=delivered` and a received Signal reminder.

3. **Ops health:** Confirm `notion_health` ran recently (should be empty
   barring real failures):
   ```bash
   docker exec -it $(docker compose ps -q postgres) \
     psql -U hml -d hml -c "SELECT * FROM ops_alerts ORDER BY created_at DESC LIMIT 5;"
   ```

4. **Logs:** Inspect `docker compose logs app` for any ERROR-level entries.

---

## 5. Revert Criteria

Trigger a rollback if ANY of these conditions are observed within 10 minutes
of starting the new stack:

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

## 6. Revert Commands

If rollback is triggered:

### 6.1 Stop the new stack

```bash
cd <repo_root>
docker compose -f docker/compose.yaml down
```

### 6.2 Restart OpenClaw (infra-managed)

Start the OpenClaw daemon using whatever mechanism was running it before:

```bash
# Example: if OpenClaw ran as a systemd user service:
systemctl --user start openclaw

# Or if run manually:
openclaw start
```

### 6.3 Verify recovery

Send a test Signal message and confirm OpenClaw responds. Check that:
- The reminder check cron is registered (`openclaw cron list`)
- state.json is intact (`cat state.json | python3 -c "import json,sys; print(json.load(sys.stdin).keys())"`)

### 6.4 Optional: restore Postgres from backup

If the Postgres data was corrupted or needs to be reset:

```bash
docker compose -f docker/compose.yaml down

docker run -d --name pg-restore \
  -e POSTGRES_USER=hml \
  -e POSTGRES_PASSWORD=hml \
  -e POSTGRES_DB=hml \
  -p 5432:5432 \
  postgres:16-alpine

until docker exec pg-restore pg_isready -U hml -d hml; do sleep 1; done

BACKUP=$(ls -t backups/postgres-*.sql.gz | head -1)
gunzip -c "$BACKUP" | docker exec -i pg-restore psql -U hml -d hml

docker stop pg-restore
docker rm pg-restore
```

---

## 7. Post-Rollback

After confirming OpenClaw is healthy:

1. File a GitHub issue describing what failed and at what step.
2. Keep the `pre-cutover-YYYYMMDD` tag in git for reference.
3. Do NOT delete `state.json.pre-cutover-YYYYMMDD` until the next cutover attempt.
4. Coordinate with the Phase D subagent / orchestrator before reattempting.
