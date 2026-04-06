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
  timeout-seconds: 60
```

This job runs as an isolated Haiku session. It is query-only: it runs the check script, which writes the reminder handoff file (default filename: `.reminder-signal`, overridable via `REMINDER_SIGNAL_FILE` in `.env`) if any reminders are due, and then exits. It does not deliver reminders to the user.

**Reminder delivery** is handled separately by two mechanisms:
1. **AGENTS.md step 5** (opportunistic): every time the user interacts, the main session checks for the handoff file and delivers immediately.
2. **HEARTBEAT.md Check 1** (guaranteed): the heartbeat reads the handoff file every 60 minutes and delivers any stranded reminders.

Both delivery paths use `scripts/notion-cli.sh complete-reminder PAGE_ID sent|missed` to atomically set Notion `Status` to `Completed`, `Reminder Status` to `sent` or `missed`, and `Completed At`.

This separation is a deliberate design choice. The previous architecture ran reminder-check on `sessionTarget: main`, which loaded the full Opus agent context (~200k tokens) for a job that 95% of the time just runs a script and finds nothing. Moving to isolated Haiku cuts per-run cost by orders of magnitude. The trade-off is that worst-case delivery latency increases from ~15 minutes to ~60 minutes when the user is fully idle, but in practice most reminders deliver on the next user interaction. The current system already can't interrupt mid-conversation (cron only fires when the REPL is idle), so the practical difference is small.

## Prompt

```
Run scripts/check-reminders.sh.
Reply with ONLY: NO_REPLY
```

## Notes

- Cron jobs auto-expire after 7 days. HEARTBEAT.md re-registers the job if missing and patches it back to this spec if the live registration drifts.
- For production deployments, 15-minute polling is the recommended cost/latency balance. For exact-time reminders such as medication, departures, or meetings, tighten the polling interval.
- Cron only fires when the REPL is idle. If the user is mid-conversation, the script won't run until the conversation pauses. For ADHD this is better — interrupting mid-task is harmful.
- `check-reminders.sh` queries Notion and writes the `.reminder-signal` handoff file. Delivery is not this job's responsibility.
