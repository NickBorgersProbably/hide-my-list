# HEARTBEAT.md

## Checks (in order)

### 1. Reminder Signal
- Check `.reminder-signal`
- If exists: read it, send each reminder to the user, update Notion status to "sent", delete the signal file

### 2. Cron Job Health
Verify that durable cron jobs are registered. If any are missing, re-register them.

| Job | Schedule | Action |
|-----|----------|--------|
| reminder-check | `*/5 * * * *` | Run `scripts/check-reminders.sh`, deliver due reminders, update Notion |
| pipeline-monitor | `*/2 * * * *` | Run `scripts/check-github-status.sh`, report actionable changes |

To check: use CronList. If a job is missing (7-day auto-expiry), re-create it with CronCreate (durable: true) using the schedule and prompt from `setup/cron/`.

### 3. Notion Connectivity
- Run `scripts/notion-cli.sh query-pending` with a short timeout
- If it fails, note the error — don't retry aggressively

### 4. Environment Check
- Verify `.env` exists and contains NOTION_API_KEY and NOTION_DATABASE_ID

### 5. Pull Main (if flagged)
- If `.pull-main` exists: `git pull origin main`, delete the flag
- If pull fails, note it — don't force

That's it. If nothing needs attention, reply HEARTBEAT_OK.
