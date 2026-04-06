# Cron Job: reminder-check

Replaces `scripts/reminder-daemon.sh`. Runs every 5 minutes via OpenClaw's durable cron.

## Registration

```
CronCreate:
  schedule: "*/5 * * * *"
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
Because the job re-enters `sessionTarget: main`, outbound routing is deterministic: deliver reminders only through the user-facing surface already attached to that main session. Do not pick a different recipient, channel, or thread. If the main session has no attached user-facing surface, leave `.reminder-signal` in place and reply with ONLY: NO_REPLY so the next eligible run can retry.

## Prompt

```
Run scripts/check-reminders.sh. That script writes .reminder-signal as the
handoff file for any due reminders. If .reminder-signal exists afterward, read
it and deliver each reminder to the user:
- On-time reminders: casual delivery ("Hey, time to [task]")
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
- Cron only fires when the REPL is idle. If the user is mid-conversation, reminders queue and deliver when the conversation pauses. For ADHD this is better — interrupting mid-task is harmful.
- `check-reminders.sh` only queries Notion and writes the handoff file; the cron prompt is what actually delivers the reminder.
