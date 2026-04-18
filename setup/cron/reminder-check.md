# Cron Job: reminder-check

Replaces `scripts/reminder-daemon.sh`. Runs every 15 minutes via OpenClaw's durable cron.

## Registration

```
CronCreate:
  schedule: "*/15 * * * *"
  durable: true
  name: "reminder-check"
  sessionTarget: isolated
  model: litellm/gemma4
  payload:
    kind: agentTurn
  timeout-seconds: 60
```

Isolated cron session. Query-only: runs check script, writes handoff file (default: `.reminder-signal`, overridable via `REMINDER_SIGNAL_FILE` in `.env`) if reminders due, exits. Does not deliver.

**Reminder delivery** — two mechanisms:
1. **AGENTS.md step 5** (opportunistic): main session checks handoff file on every user interaction.
2. **HEARTBEAT.md Check 1** (hourly backstop): heartbeat reads handoff file every 60 min, delivers stranded reminders.

Both paths validate before sending: must be JSON with `reminders` array, each entry has string `page_id`, non-empty string `title`, `status` exactly `sent` or `missed`. Malformed → leave file, no delivery, no `complete-reminder` call, no delete. Valid → send each via OpenClaw `message` tool (`action: send`, `channel: signal`), then call `scripts/notion-cli.sh complete-reminder PAGE_ID sent|missed` to atomically set Notion `Status → Completed`, `Reminder Status → sent|missed`, `Completed At`.

Deliberate design: old arch used `sessionTarget: main`, loaded full Opus context (~200k tokens) for a job that 95% finds nothing. Isolated cron with `litellm/gemma4` cuts cost by orders of magnitude. Trade-off: idle worst-case latency ~75 min (15 min cron + 60 min heartbeat). In practice most reminders still deliver on next user interaction. Practical difference small — cron only fires when REPL idle anyway.

Handoff file = durability boundary. OpenClaw lacks post-announce delivery ack hook, so announce-only flow can't safely call `complete-reminder` before delivery without risking loss on crash. Job stays query-only until hook exists.

## Prompt

```
Run scripts/check-reminders.sh.
Reply with ONLY: NO_REPLY
```

## Notes

- Cron auto-expires after 7 days. HEARTBEAT.md re-registers if missing, patches if drifted.
- 15-min polling = recommended cost/latency balance. Affects discovery only; idle delivery still depends on heartbeat or next interaction.
- Cron fires only when REPL idle. Mid-conversation → script waits. Better for ADHD — no mid-task interrupts.
- `check-reminders.sh` queries Notion, writes `.reminder-signal`. Delivery not this job's responsibility.