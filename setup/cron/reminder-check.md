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
  delivery:
    mode: announce
  payload:
    kind: agentTurn
  timeout-seconds: 120
```

This job runs as an isolated Haiku session and is the single reminder-delivery path. It runs `scripts/check-reminders.sh` to query Notion for due reminders, formats any due reminders as user-facing text, uses `scripts/notion-cli.sh complete-reminder PAGE_ID sent|missed` for each reminder it actually delivers, and relies on `delivery.mode: announce` for OpenClaw to send the final reply to the user.

If no reminders are due, the session replies `NO_REPLY`, which OpenClaw suppresses for announce delivery. This keeps the no-op path cheap while removing the old intermediate file flow, startup reminder check, and heartbeat delivery backstop.

This keeps the cost profile from the isolated Haiku design while improving the idle-user delivery path. The worst-case latency is now the cron discovery interval (about 15 minutes, plus any time spent waiting for the REPL to become idle) instead of cron discovery plus an extra delivery hop.

## Prompt

```
Run `scripts/check-reminders.sh` and parse its JSON output.

If `reminders` is empty, reply with ONLY:
NO_REPLY

If reminders are present:
- Deliver them in one brief message.
- For `status: sent`, use a casual shoulder-tap tone.
- For `status: missed`, note the delay without shame.
- After deciding to deliver a reminder, run `scripts/notion-cli.sh complete-reminder PAGE_ID STATUS` for that reminder.
- Only include reminders in the final reply after their `complete-reminder` call succeeds.
- If every `complete-reminder` call fails, reply with ONLY:
NO_REPLY

Reply with only the reminder text for the reminders you delivered.
```

## Notes

- Cron jobs auto-expire after 7 days. HEARTBEAT.md re-registers the job if missing and patches it back to this spec if the live registration drifts.
- For production deployments, 15-minute polling is the recommended cost/latency balance for routine reminders.
- Cron only fires when the REPL is idle. If the user is mid-conversation, the script won't run until the conversation pauses. For ADHD this is better — interrupting mid-task is harmful.
- `check-reminders.sh` is a query helper. The cron turn owns delivery and completion updates end-to-end.
