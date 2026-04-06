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

This job injects a `systemEvent` into the main agent session instead of spawning an isolated cron-specific sub-agent. Delivery is `mode: none` because hide-my-list should decide whether to speak at all, while keeping delivery on the conversation surface already attached to `main`. The 120s timeout gives the LLM enough time to process the full agent context.
Because the job re-enters `sessionTarget: main`, outbound routing is deterministic: deliver reminders only through the user-facing surface already attached to that main session. Do not pick a different recipient, channel, or thread. Resolve the reminder handoff path with the same shell helper the scripts use so `.env` overrides are honored:
`HANDOFF_FILE=$(bash -lc 'SCRIPT_DIR=$(cd "$(dirname scripts/check-reminders.sh)" && pwd); ROOT_DIR=$(cd "$SCRIPT_DIR/.." && pwd); source "$SCRIPT_DIR/load-env.sh" REMINDER_SIGNAL_FILE?; printf "%s\n" "${REMINDER_SIGNAL_FILE:-$ROOT_DIR/.reminder-signal}"')`
If the main session has no attached user-facing surface, leave `HANDOFF_FILE` in place and reply with ONLY: NO_REPLY so the next eligible run can retry.
Because it re-enters `main`, `reminder-check` also uses the main session's configured primary conversation model rather than selecting a separate cheap-worker model. That is intentional in the current architecture: deterministic delivery on the existing user surface matters more than isolated cron-only model savings.

## Prompt

```
Run scripts/check-reminders.sh. That script writes the reminder handoff file
for any due reminders. After it runs, resolve the handoff path with the same
shell helper the scripts use so `.env` overrides are honored:
HANDOFF_FILE=$(bash -lc 'SCRIPT_DIR=$(cd "$(dirname scripts/check-reminders.sh)" && pwd); ROOT_DIR=$(cd "$SCRIPT_DIR/.." && pwd); source "$SCRIPT_DIR/load-env.sh" REMINDER_SIGNAL_FILE?; printf "%s\n" "${REMINDER_SIGNAL_FILE:-$ROOT_DIR/.reminder-signal}"')
If `HANDOFF_FILE` exists afterward, read it and deliver each reminder to the user:
- Approximate reminders (next eligible poll, before missed threshold): casual delivery ("Hey, time to [task]")
- Missed reminders (>15 min late): note the delay but don't shame ("This was due a bit
  ago — [task]")
After delivery, read each reminder's status from the handoff file and update Notion accordingly:
  If status is "sent":
    scripts/notion-cli.sh update-property PAGE_ID '{"properties":{"Status":{"select":{"name":"Completed"}},"Reminder Status":{"select":{"name":"sent"}}}}'
  If status is "missed":
    scripts/notion-cli.sh update-property PAGE_ID '{"properties":{"Status":{"select":{"name":"Completed"}},"Reminder Status":{"select":{"name":"missed"}}}}'
Delete `HANDOFF_FILE` only after every reminder was delivered and its Notion `Status` plus `Reminder Status` were updated.
If delivery fails before that point, leave `HANDOFF_FILE` in place and do not mark the affected reminder as sent or missed.
If there is nothing to report, reply with ONLY: NO_REPLY
```

## Notes

- Cron jobs auto-expire after 7 days. HEARTBEAT.md re-registers the job if missing and patches it back to this spec if the live registration drifts.
- For production deployments, 15-minute polling is the recommended cost/latency balance for routine reminders. Reminder delivery is the time-sensitive path; heartbeat remains the hourly safety net. For exact-time reminders such as medication, departures, or meetings, tighten the polling interval.
- Cron only fires when the REPL is idle. If the user is mid-conversation, reminders queue and deliver when the conversation pauses. For ADHD this is better — interrupting mid-task is harmful.
- `check-reminders.sh` only queries Notion and writes the handoff file; the cron prompt is what actually delivers the reminder.
