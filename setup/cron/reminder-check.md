# Cron Job: reminder-check

Replaces `scripts/reminder-daemon.sh`. Runs every 15 minutes via OpenClaw's durable cron.

## Registration

```
CronCreate:
  schedule: "*/15 * * * *"
  durable: true
  name: "reminder-check"
  sessionTarget: isolated
  model: litellm/claude-haiku-4-5
  payload:
    kind: agentTurn
  delivery:
    mode: none
  timeout-seconds: 120
```

This job runs as an isolated `agentTurn` on `litellm/claude-haiku-4-5` instead of waking the main Opus/Sonnet conversation session for every idle cron poll. Delivery stays `mode: none` so the prompt decides whether to speak at all, and the 120s timeout still leaves enough room for the cron turn to run `scripts/check-reminders.sh`, inspect `.reminder-signal`, and finish any reminder handoff work.

## Prompt

```
Run scripts/check-reminders.sh. That script writes .reminder-signal as the
handoff file for any due reminders. If .reminder-signal exists afterward, read
it and deliver each reminder to the user:
- Approximate reminders (next eligible poll, before missed threshold): casual delivery ("Hey, time to [task]")
- Missed reminders (>15 min late): note the delay but don't shame ("This was due a bit
  ago — [task]")
After delivery, read each reminder's status from .reminder-signal and update Notion accordingly:
  If status is "sent":
    scripts/notion-cli.sh update-property PAGE_ID '{"properties":{"Reminder Status":{"select":{"name":"sent"}}}}'
  If status is "missed":
    scripts/notion-cli.sh update-property PAGE_ID '{"properties":{"Reminder Status":{"select":{"name":"missed"}}}}'
Delete .reminder-signal only after every reminder was delivered and its Notion status was updated.
If delivery fails before that point, leave .reminder-signal in place and do not mark the affected reminder as sent or missed.
If there is nothing to report, reply with ONLY: NO_REPLY
```

## Notes

- Cron jobs auto-expire after 7 days. HEARTBEAT.md re-registers the job if missing and patches it back to this spec if the live registration drifts.
- For production deployments, 15-minute polling is the recommended cost/latency balance for routine reminders. Reminder delivery is the time-sensitive path; heartbeat remains the hourly safety net. For exact-time reminders such as medication, departures, or meetings, tighten the polling interval.
- Cron only fires when the REPL is idle. If the user is mid-conversation, reminders queue and deliver when the conversation pauses. For ADHD this is better — interrupting mid-task is harmful.
- `check-reminders.sh` only queries Notion and writes the handoff file; the cron prompt is what actually delivers the reminder.
