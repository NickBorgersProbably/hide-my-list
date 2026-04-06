# Cron Job: reminder-check

Replaces `scripts/reminder-daemon.sh`. Runs every 15 minutes via OpenClaw's durable cron.

## Registration

```
CronCreate:
  schedule: "*/15 * * * *"
  durable: true
  name: "reminder-check"
  sessionTarget: main
  payload:
    kind: systemEvent
  delivery:
    mode: none
  timeout-seconds: 120
```

This job injects a `systemEvent` into the bound `main` session instead of spawning an isolated cron-only turn. Delivery stays `mode: none` so the prompt decides whether to speak at all, while keeping reminder output pinned to the existing user-facing surface already attached to `main`. The 120s timeout still leaves enough room to run `scripts/check-reminders.sh`, inspect `.reminder-signal`, and finish the reminder handoff.

## Prompt

```
Run scripts/check-reminders.sh. That script writes .reminder-signal as the
handoff file for any due reminders. If .reminder-signal exists afterward, read
it and deliver each reminder to the user through the user-facing surface already
attached to the `main` session. Do not pick a different recipient, channel, or
thread. If no user-facing surface is attached to `main`, leave .reminder-signal
in place and reply with ONLY: NO_REPLY.
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
