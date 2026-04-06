# Cron Job: reminder-delivery

Agentic reminder delivery. Runs every 15 minutes, offset a couple minutes after `reminder-check`, and only spends meaningful tokens when `.reminder-signal` exists.

## Registration

```
CronCreate:
  schedule: "2,17,32,47 * * * *"
  durable: true
  name: "reminder-delivery"
  sessionTarget: main
  payload:
    kind: agentTurn
  delivery:
    mode: none
  model: "litellm/claude-haiku-4-5"
  timeout-seconds: 120
```

This job re-enters the `main` session with an ordinary agent turn, pinned to `litellm/claude-haiku-4-5` so the empty-check path stays cheap. Because it targets `main`, outbound routing is deterministic: deliver reminders only through the user-facing surface already attached to that session. Do not pick a different recipient, channel, or thread.

## Prompt

```
If .reminder-signal does not exist, reply with ONLY: NO_REPLY

If .reminder-signal exists:
1. Read it and deliver each reminder to the user on the existing `main` session surface.
2. On-time reminders: casual delivery ("Hey, time to [task]").
3. Missed reminders (`status: "missed"`): note the delay without shame.
4. After delivery, update Notion for each reminder:
   If status is "sent":
     scripts/notion-cli.sh update-property PAGE_ID '{"properties":{"Reminder Status":{"select":{"name":"sent"}}}}'
   If status is "missed":
     scripts/notion-cli.sh update-property PAGE_ID '{"properties":{"Reminder Status":{"select":{"name":"missed"}}}}'
5. Delete .reminder-signal only after every reminder was delivered and its Notion status was updated.

If the main session has no attached user-facing surface, reply with ONLY: NO_REPLY and leave .reminder-signal in place for the next eligible run.
If delivery fails before that point, leave .reminder-signal in place and do not mark the affected reminder as sent or missed.
```

## Notes

- The common case should be nearly free: check for `.reminder-signal`, emit `NO_REPLY`, exit.
- Heartbeat can still recover stranded `.reminder-signal` files if the delivery cron fails or the `main` session is unavailable for a while.
- Cron jobs auto-expire after 7 days. HEARTBEAT.md re-registers the job if missing and patches it back to this spec if the live registration drifts.
