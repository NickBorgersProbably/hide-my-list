# HEARTBEAT.md

## Checks (in order)

### 1. Stranded Reminder Signal
- Resolve the reminder handoff file with the same helper and repo-root filename validation the scripts use, so `.env` overrides are honored:
  ```bash
  HANDOFF_FILE="$(bash -lc 'SCRIPT_DIR=$(cd "$(dirname scripts/check-reminders.sh)" && pwd); ROOT_DIR=$(cd "$SCRIPT_DIR/.." && pwd); source "$SCRIPT_DIR/load-env.sh" REMINDER_SIGNAL_FILE?; SIGNAL_BASENAME=${REMINDER_SIGNAL_FILE:-.reminder-signal}; case "$SIGNAL_BASENAME" in ""|"."|".."|*/*) echo "invalid REMINDER_SIGNAL_FILE" >&2; exit 1 ;; esac; printf "%s\n" "$ROOT_DIR/$SIGNAL_BASENAME"')"
  ```
- This file is the reminder handoff written by `scripts/check-reminders.sh`
- If `HANDOFF_FILE` still exists when heartbeat runs, treat it as undelivered reminder work: read it, send each reminder to the user, run `scripts/notion-cli.sh complete-reminder PAGE_ID sent|missed` based on the file, then delete that handoff file
- This is the hourly reminder-delivery backstop in the current design. The isolated `reminder-check` cron only writes the handoff file — it does not deliver. Delivery happens here (every 60 min) and opportunistically via the AGENTS.md startup check (on every user interaction).

### 2. Cron Job Health
Verify that durable cron jobs are registered. If any are missing, re-register them.

| Job | Schedule | Action |
|-----|----------|--------|
| reminder-check | `*/15 * * * *` | Run `scripts/check-reminders.sh` (query-only; writes the reminder handoff file if reminders due) |
| pull-main | `*/10 * * * *` | Run `scripts/pull-main.sh`; the script handles dirty-pull recovery |

To check: use CronList. If a job is missing (7-day auto-expiry), re-create it with CronCreate (durable: true) using the schedule, prompt, and options from `setup/cron/`. Both jobs run as isolated Haiku sessions with `sessionTarget: isolated`, `model: litellm/claude-haiku-4-5`, `payload.kind: agentTurn`, and `timeout-seconds: 60`. Cron jobs should never deliver directly to Signal or any other channel on their own.

### 2b. Cron Spec Drift Check
For each registered cron job (`reminder-check`, `pull-main`), compare the live job's effective registration against the canonical `CronCreate` spec in `setup/cron/<name>.md`.

To check: use CronList to inspect the live registrations, then read the corresponding spec file in `setup/cron/`.

At minimum, compare and correct these fields:
- `name`
- `durable`
- `schedule`
- `prompt`
- `sessionTarget` (canonical: `isolated` for both jobs)
- `model` (canonical: `litellm/claude-haiku-4-5` for both jobs)
- direct-delivery routing field: live `to` if present (should not exist)
- payload field: canonical `payload.kind`
- `timeout-seconds`

If a stale `pipeline-monitor` cron is still registered, delete it with CronDelete — that job has been removed.

If any field differs from the spec, patch the live job to match with CronUpdate. If CronUpdate cannot safely change an identity field such as `name` or `durable`, delete and re-create the job from the spec instead of leaving drift in place. Preserve the intended durable registration contract from the spec:
- `reminder-check`: `name`, `durable`, `schedule`, `prompt`, `sessionTarget: isolated`, `model: litellm/claude-haiku-4-5`, no `to`, `payload.kind: agentTurn`, `timeout-seconds: 60`
- `pull-main`: `name`, `durable`, `schedule`, `prompt`, `sessionTarget: isolated`, `model: litellm/claude-haiku-4-5`, no `to`, `payload.kind: agentTurn`, `timeout-seconds: 60`

If all jobs already match their specs, do not report anything. If any jobs were corrected, briefly note which ones were patched and what drift was fixed.

### 3. Notion Connectivity
- Run `scripts/notion-cli.sh query-pending` with a short timeout
- If it fails, note the error — don't retry aggressively

### 4. Environment Check
- Verify `.env` exists and contains NOTION_API_KEY and NOTION_DATABASE_ID

### 5. Dirty Pull Recovery (safety net)
- If `.pull-dirty` exists and is older than 20 minutes, the pull-main cron may have failed to recover
- Run `scripts/pull-main.sh --recover-only` to retry after the underlying problem is fixed (for example restoring interactive `gh` authentication, exporting a valid `GH_TOKEN`, or providing token-based auth through repo `.env` `GITHUB_PAT`, which the helper exports as `GH_TOKEN`). The script creates the GitHub issue and resets the repo when recovery can proceed.
- If recovery still does not complete, note the failure for operator attention
- Normally the pull-main cron handles recovery automatically. This check is a backstop for cases where GitHub auth was unavailable or the script errored; until either interactive `gh` auth, a valid exported `GH_TOKEN`, or repo `.env` `GITHUB_PAT` is available again, heartbeat will preserve `.pull-dirty` and surface the problem rather than clearing it

That's it. If nothing needs attention, reply HEARTBEAT_OK.
