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
| pipeline-monitor | `*/2 * * * *` | Run `scripts/check-github-status.sh`, report actionable changes |
| pull-main | `*/10 * * * *` | Run `scripts/pull-main.sh`, handle dirty pulls |

To check: use CronList. If a job is missing (7-day auto-expiry), re-create it with CronCreate (durable: true) using the schedule, prompt, and options from `setup/cron/`. All cron jobs must include `best-effort-deliver: true`, `to: $SIGNAL_OWNER_NUMBER` (from `.env`), and `timeout-seconds: 120`.

### 3. Notion Connectivity
- Run `scripts/notion-cli.sh query-pending` with a short timeout
- If it fails, note the error — don't retry aggressively

### 4. Environment Check
- Verify `.env` exists and contains NOTION_API_KEY, NOTION_DATABASE_ID, and SIGNAL_OWNER_NUMBER

### 5. Dirty Pull Recovery (safety net)
- If `.pull-dirty` exists and is older than 20 minutes, the pull-main cron may have failed to handle it
- Read `.pull-dirty` for context about what files changed and why
- Create a GitHub issue on NickBorgersProbably/hide-my-list documenting the local changes (include the full diff so nothing is lost)
- Check memory/ for any notes about why these changes were made — include that context in the issue
- Reset to match remote: `git checkout -- . && git clean -fd && git pull origin main`
- Delete `.pull-dirty`
- Normally the pull-main cron handles this automatically — this check is a backstop

That's it. If nothing needs attention, reply HEARTBEAT_OK.
