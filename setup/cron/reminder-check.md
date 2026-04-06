# Cron Job: reminder-check

Procedural reminder polling. Runs every 15 minutes via OpenClaw's durable cron and never spends LLM tokens on reminder delivery.

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

This job injects a `systemEvent` into the main agent session, just like `pull-main`. It exists only to run the procedural reminder poller. Delivery is `mode: none` because this job should always stay silent; any user-visible reminder delivery belongs to the separate `reminder-delivery` cron job.

## Prompt

```
Run scripts/check-reminders.sh.
Reply with ONLY: NO_REPLY
```

## Notes

- Cron jobs auto-expire after 7 days. HEARTBEAT.md re-registers the job if missing and patches it back to this spec if the live registration drifts.
- For production deployments, 15-minute polling is the recommended cost/latency balance for routine reminders. Reminder delivery is the time-sensitive path; heartbeat remains the hourly safety net. For exact-time reminders such as medication, departures, or meetings, tighten the polling interval.
- This job should run a couple minutes before `reminder-delivery` so `.reminder-signal` is ready when the agentic delivery guard checks.
- Cron only fires when the REPL is idle. If the user is mid-conversation, reminders queue and deliver when the conversation pauses. For ADHD this is better — interrupting mid-task is harmful.
- `check-reminders.sh` only queries Notion and writes the handoff file. It does not deliver reminders or update reminder status in Notion.
