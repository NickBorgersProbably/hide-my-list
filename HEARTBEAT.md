# HEARTBEAT.md

## Checks (in order)

### 1. Stranded Reminder Signal
- Check `.reminder-signal`
- This file is the reminder handoff written by `scripts/check-reminders.sh`
- If it still exists when heartbeat runs, treat it as undelivered reminder work: read it, send each reminder to the user, update Notion status to `sent` or `missed` based on the file, then delete the signal file

### 2. Cron Job Health
Verify that durable cron jobs are registered. If any are missing, re-register them.

| Job | Schedule | Action |
|-----|----------|--------|
| reminder-check | `*/5 * * * *` | Run `scripts/check-reminders.sh`; if it writes `.reminder-signal`, read it, deliver reminders, update Notion, delete the file |
| pull-main | `*/10 * * * *` | Run `scripts/pull-main.sh`; the script handles dirty-pull recovery |

To check: use CronList. If a job is missing (7-day auto-expiry), re-create it with CronCreate (durable: true) using the schedule, prompt, and options from `setup/cron/`. Both jobs must inject into the main agent session with `sessionTarget: main`, `payload.kind: systemEvent`, `delivery.mode: none`, and `timeout-seconds: 120`. Cron jobs should never deliver directly to Signal or any other channel on their own.

### 2b. Cron Spec Drift Check
For each registered cron job (`reminder-check`, `pull-main`), compare the live job's `prompt`, `schedule`, session target (if any), payload kind, delivery mode, and timeout against the canonical spec in `setup/cron/<name>.md`.

To check: use CronList to inspect the live registrations, then read the corresponding spec file in `setup/cron/`.

If a stale `pipeline-monitor` cron is still registered, delete it with CronDelete — that job has been removed.

If any field differs from the spec, patch the live job to match with CronUpdate. Preserve the intended durable registration contract from the spec:
- `reminder-check`: `schedule`, `prompt`, `sessionTarget: main`, `payload.kind: systemEvent`, `delivery.mode: none`, `timeout-seconds: 120`
- `pull-main`: `schedule`, `prompt`, `sessionTarget: main`, `payload.kind: systemEvent`, `delivery.mode: none`, `timeout-seconds: 120`

If all jobs already match their specs, do not report anything. If any jobs were corrected, briefly note which ones were patched and what drift was fixed.

### 3. Notion Connectivity
- Run `scripts/notion-cli.sh query-pending` with a short timeout
- If it fails, note the error — don't retry aggressively

### 4. Environment Check
- Verify `.env` exists and contains NOTION_API_KEY and NOTION_DATABASE_ID

### 5. Dirty Pull Recovery (safety net)
- If `.pull-dirty` exists and is older than 20 minutes, the pull-main cron may have failed to recover
- Run `scripts/pull-main.sh --recover-only` to retry after the underlying problem is fixed (for example restoring `gh` authentication). The script creates the GitHub issue and resets the repo when recovery can proceed.
- If recovery still does not complete, note the failure for operator attention
- Normally the pull-main cron handles recovery automatically. This check is a backstop for cases where `gh` was not authenticated or the script errored; until `gh` auth is restored, heartbeat will preserve `.pull-dirty` and surface the problem rather than clearing it

That's it. If nothing needs attention, reply HEARTBEAT_OK.
