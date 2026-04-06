# HEARTBEAT.md

## Checks (in order)

### 1. Stranded Reminder Signal
- Check `.reminder-signal`
- This file is the reminder handoff written by `scripts/check-reminders.sh`
- If it still exists when heartbeat runs, treat it as undelivered reminder work: read it, classify each reminder as `sent` or `missed` by comparing the current time against the reminder's `remind_at` timestamp (more than 15 minutes late = `missed`), deliver each reminder to the user, update Notion `ReminderStatus` accordingly, then delete the signal file
- Do NOT update the main task `Status` to `Completed` — reminder delivery notifies the user; task completion is a separate user action

### 2. Cron Job Health
Verify that durable cron jobs are registered. If any are missing, re-register them.

| Job | Schedule | Action |
|-----|----------|--------|
| reminder-check | `*/15 * * * *` | Run `scripts/check-reminders.sh`; writes `.reminder-signal` if reminders are due |
| reminder-delivery | `2,17,32,47 * * * *` | Check for `.reminder-signal`; if present, deliver reminders, update Notion `ReminderStatus`, delete file |
| pull-main | `*/10 * * * *` | Run `scripts/pull-main.sh`; the script handles dirty-pull recovery |

To check: use CronList. If a job is missing (7-day auto-expiry), re-create it with CronCreate (durable: true) using the schedule, prompt, and options from `setup/cron/`. `reminder-check` and `pull-main` inject into the main agent session with `sessionTarget: main`, `payload.kind: systemEvent`, `delivery.mode: none`, and `timeout-seconds: 120`. `reminder-delivery` runs in an isolated session (no `sessionTarget`) with `payload.kind: agentTurn`, `model: litellm/claude-haiku-4-5`, `delivery.mode: best-effort-deliver`, and `timeout-seconds: 120`.

### 2b. Cron Spec Drift Check
For each registered cron job (`reminder-check`, `reminder-delivery`, `pull-main`), compare the live job's effective registration against the canonical `CronCreate` spec in `setup/cron/<name>.md`.

To check: use CronList to inspect the live registrations, then read the corresponding spec file in `setup/cron/`.

At minimum, compare and correct these fields:
- `name`
- `durable`
- `schedule`
- `prompt`
- session routing field: canonical `sessionTarget` (when specified)
- direct-delivery routing field: live `to` if present
- payload field: canonical `payload.kind`
- delivery behavior fields: canonical `delivery.mode` and any equivalent live field such as `best-effort-deliver`
- `model` (when the spec pins a model, e.g. `reminder-delivery`)
- `timeout-seconds`

`to` is not a legacy spelling of `sessionTarget`. `sessionTarget` controls whether the cron run re-enters `main`, while `to` is direct-delivery routing for isolated jobs. For `reminder-check` and `pull-main`, the canonical contract is `sessionTarget: main` with no direct-delivery target, so any populated `to` should be treated as drift and removed rather than accepted as equivalent. `reminder-delivery` is intentionally isolated (no `sessionTarget`) and uses `delivery.mode: best-effort-deliver`.

If a stale `pipeline-monitor` cron is still registered, delete it with CronDelete — that job has been removed.

`pull-main` now handles the fast path after clean pulls that advance `HEAD`: it immediately reapplies any changed `setup/cron/` specs from that invocation's commit range. Heartbeat remains the safety net for expired jobs, missed fast-path updates, and any residual drift.

If any field differs from the spec, patch the live job to match with CronUpdate. If CronUpdate cannot safely change an identity field such as `name` or `durable`, delete and re-create the job from the spec instead of leaving drift in place. Preserve the intended durable registration contract from the spec:
- `reminder-check`: `name`, `durable`, `schedule`, `prompt`, `sessionTarget: main`, no `to`, `payload.kind: systemEvent`, `delivery.mode: none`, `timeout-seconds: 120`
- `reminder-delivery`: `name`, `durable`, `schedule`, `prompt`, no `sessionTarget`, `payload.kind: agentTurn`, `model: litellm/claude-haiku-4-5`, `delivery.mode: best-effort-deliver`, `timeout-seconds: 120`
- `pull-main`: `name`, `durable`, `schedule`, `prompt`, `sessionTarget: main`, no `to`, `payload.kind: systemEvent`, `delivery.mode: none`, `timeout-seconds: 120`

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
