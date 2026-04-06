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
1. **AGENTS.md step 5** (opportunistic): every time the user interacts, the main session atomically claims the handoff file and delivers immediately.
2. **HEARTBEAT.md Check 1** (hourly backstop): the heartbeat atomically claims the handoff file every 60 minutes and delivers any stranded reminders.

Both delivery paths use `scripts/notion-cli.sh complete-reminder PAGE_ID sent|missed` to atomically set Notion `Status` to `Completed`, `Reminder Status` to `sent` or `missed`, and `Completed At`.

This separation is a deliberate design choice. The previous architecture ran reminder-check on `sessionTarget: main`, which loaded the full Opus agent context (~200k tokens) for a job that 95% of the time just runs a script and finds nothing. Moving to isolated Haiku cuts per-run cost by orders of magnitude. The trade-off is that fully idle worst-case delivery latency is now up to about 75 minutes: up to 15 minutes for this cron to write the handoff file, then up to another 60 minutes for heartbeat to deliver it if the user does not interact first. In practice, many reminders still deliver on the next user interaction. The current system already can't interrupt mid-conversation (cron only fires when the REPL is idle), so the practical difference is small.

## Prompt

```
Run scripts/check-reminders.sh.
Reply with ONLY: NO_REPLY
```

## Notes

- Cron jobs auto-expire after 7 days. HEARTBEAT.md re-registers the job if missing and patches it back to this spec if the live registration drifts.
- For production deployments, 15-minute polling is the recommended cost/latency balance for routine reminders. This affects discovery time only; idle delivery still depends on the hourly heartbeat or the next user interaction.
- Cron only fires when the REPL is idle. If the user is mid-conversation, the script won't run until the conversation pauses. For ADHD this is better — interrupting mid-task is harmful.
- `check-reminders.sh` queries Notion and writes the `.reminder-signal` handoff file. Delivery is not this job's responsibility.
