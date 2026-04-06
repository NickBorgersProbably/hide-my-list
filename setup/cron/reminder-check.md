# Cron Job: reminder-check

Procedural polling job that queries Notion for due reminders and writes a signal file. Runs every 15 minutes via OpenClaw's durable cron. This job does **not** deliver reminders — the separate `reminder-delivery` job handles that.

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

This job injects a `systemEvent` into the main agent session. Delivery is `mode: none` because the job is silent — it only runs the polling script and exits. The 120s timeout gives the script enough time to query Notion with retries.

## Prompt

```
Run scripts/check-reminders.sh. Reply with ONLY: NO_REPLY
```

The script queries Notion for due reminders and writes `.reminder-signal` if any are found. The separate `reminder-delivery` cron job (offset by 2 minutes) picks up that signal file and handles user-facing delivery, Notion status updates, and signal cleanup.

## Notes

- Cron jobs auto-expire after 7 days. HEARTBEAT.md re-registers the job if missing and patches it back to this spec if the live registration drifts.
- This job is purely procedural: `check-reminders.sh` does all the work, and the LLM prompt is minimal (run script, say NO_REPLY). The cost-intensive reminder delivery logic lives in the separate `reminder-delivery` job, which is pinned to Haiku.
- For production deployments, 15-minute polling is the recommended cost/latency balance for routine reminders. For exact-time reminders such as medication, departures, or meetings, tighten the polling interval.
- Cron only fires when the REPL is idle. If the user is mid-conversation, the poll queues until the conversation pauses — better for ADHD focus.
