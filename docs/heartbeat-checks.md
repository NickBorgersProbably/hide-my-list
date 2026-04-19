# Heartbeat Checks

## Checks (in order)

### 1. Stranded Reminder Signal
- Resolve reminder handoff file using same helper + repo-root filename validation scripts use, so `.env` overrides honored:
  ```bash
  HANDOFF_FILE="$(bash -lc 'SCRIPT_DIR=$(cd "$(dirname scripts/check-reminders.sh)" && pwd); ROOT_DIR=$(cd "$SCRIPT_DIR/.." && pwd); source "$SCRIPT_DIR/load-env.sh" REMINDER_SIGNAL_FILE?; SIGNAL_BASENAME=${REMINDER_SIGNAL_FILE:-.reminder-signal}; case "$SIGNAL_BASENAME" in ""|"."|".."|*/*) echo "invalid REMINDER_SIGNAL_FILE" >&2; exit 1 ;; esac; printf "%s\n" "$ROOT_DIR/$SIGNAL_BASENAME"')"
  ```
- Resolve heartbeat ops-alert Signal recipient from `.env` before any alert send:
  ```bash
  OPS_ALERT_TARGET="$(bash -lc 'SCRIPT_DIR=$(cd "$(dirname scripts/check-reminders.sh)" && pwd); source "$SCRIPT_DIR/load-env.sh" OPS_ALERT_SIGNAL_NUMBER?; if [ -z "${OPS_ALERT_SIGNAL_NUMBER:-}" ]; then echo "missing OPS_ALERT_SIGNAL_NUMBER" >&2; exit 1; fi; printf "%s\n" "$OPS_ALERT_SIGNAL_NUMBER"')"
  ```
- File = reminder handoff written by `scripts/check-reminders.sh`
- If `HANDOFF_FILE` exists at heartbeat time: treat as undelivered. Read + validate (must be JSON with `reminders` array; each entry needs string `page_id`, non-empty string `title`, `status` exactly `sent` or `missed`; any other shape/status = malformed → leave file, send ops alert via `message` tool (`action: send`, `channel: signal`, `target: "$OPS_ALERT_TARGET"`) describing the malformed handoff, skip delivery/`complete-reminder`/delete). For each valid reminder: send via OpenClaw `message` tool (`action: send`, `channel: signal`), run `scripts/notion-cli.sh complete-reminder PAGE_ID sent|missed`, delete handoff file.
- Hourly delivery backstop. Isolated `reminder-check` cron only writes handoff — no delivery. Delivery here (every 60 min) + opportunistically via AGENTS.md startup check.

### 2. Cron Job Health
Verify durable cron jobs registered. Re-register any missing.

| Job | Schedule | Action |
|-----|----------|--------|
| reminder-check | `*/15 * * * *` | Run `scripts/check-reminders.sh` (query-only; writes reminder handoff if reminders due) |
| pull-main | `*/10 * * * *` | Run `scripts/pull-main.sh`; script handles dirty-pull recovery |

Check via CronList. Missing (7-day auto-expiry) → re-create with CronCreate (durable: true) using schedule, prompt, options from `setup/cron/`. Both jobs: `sessionTarget: isolated`, `model: litellm/gemma4-small`, `payload.kind: agentTurn`, `timeout-seconds: 60`. Cron jobs never deliver directly to Signal or other channels.

### 2b. Cron Spec Drift Check
For each registered cron job (`reminder-check`, `pull-main`), compare live registration against canonical `CronCreate` spec in `setup/cron/<name>.md`.

Check: CronList for live registrations, read spec files in `setup/cron/`.

Compare + correct these fields:
- `name`
- `durable`
- `schedule`
- `prompt`
- `sessionTarget` (canonical: `isolated` both jobs)
- `model` (canonical: `litellm/gemma4-small` both jobs)
- direct-delivery routing: live `to` if present (should not exist)
- payload: canonical `payload.kind`
- `timeout-seconds`

Stale `pipeline-monitor` cron still registered → delete with CronDelete (job removed).

Field differs from spec → patch with CronUpdate. Identity field (`name`, `durable`) can't be safely changed → delete + re-create from spec. Intended contract:
- `reminder-check`: `name`, `durable`, `schedule`, `prompt`, `sessionTarget: isolated`, `model: litellm/gemma4-small`, no `to`, `payload.kind: agentTurn`, `timeout-seconds: 60`
- `pull-main`: `name`, `durable`, `schedule`, `prompt`, `sessionTarget: isolated`, `model: litellm/gemma4-small`, no `to`, `payload.kind: agentTurn`, `timeout-seconds: 60`

All match → report nothing. Any corrected → note which + what drift fixed.

### 3. Notion Connectivity
- Run `scripts/notion-cli.sh query-pending` with short timeout
- Fails → send ops alert via `message` tool (`action: send`, `channel: signal`, `target: "$OPS_ALERT_TARGET"`) with error detail, no aggressive retry

### 4. Environment Check
- Verify `.env` exists + contains NOTION_API_KEY and NOTION_DATABASE_ID

### 5. Dirty Pull Recovery (safety net)
- `.pull-dirty` exists + older than 20 min → pull-main cron may have failed recovery
- Run `scripts/pull-main.sh --recover-only` after fixing underlying problem (restore interactive `gh` auth, export valid `GH_TOKEN`, or provide `GITHUB_PAT` in repo `.env` — helper exports as `GH_TOKEN`). Script creates GitHub issue + resets repo when recovery can proceed.
- Recovery still fails → send ops alert via `message` tool (`action: send`, `channel: signal`, `target: "$OPS_ALERT_TARGET"`) describing the failure
- Normally pull-main cron handles recovery. This backstop for cases where GitHub auth was unavailable or script errored; until `gh` auth, valid `GH_TOKEN`, or `GITHUB_PAT` available, heartbeat preserves `.pull-dirty` + surfaces problem

Nothing needs attention → reply HEARTBEAT_OK.
