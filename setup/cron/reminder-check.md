# Cron Job: reminder-check

Replaces `scripts/reminder-daemon.sh`. Runs every 15 minutes via OpenClaw's durable cron.

## Registration

```
CronCreate:
  schedule: "*/15 * * * *"
  durable: true
  name: "reminder-check"
  sessionTarget: isolated
  model: litellm/gemma4-small  # must match modelTiers.cheap
  payload:
    kind: agentTurn
    lightContext: true  # empty bootstrap — cron prompt is self-contained
  timeout-seconds: 300
```

Isolated cheap-tier session (see `modelTiers` in `setup/openclaw.json.template`). Query-only: runs check script, writes handoff file (default: `.reminder-signal`, overridable via `REMINDER_SIGNAL_FILE` in `.env`) if reminders due, exits. Does not deliver.

**Role: backstop.** Primary reminder delivery is the per-reminder one-shot cron registered at intake (`setup/cron/reminder-delivery.md`). This `reminder-check` polling job catches anything that primary path misses: `CronCreate` failures at intake, jobs that fail to fire (gateway down at the scheduled time, etc.), or reminders saved before the one-shot architecture shipped.

**Backstop delivery paths** (only fire when this poll finds a still-Pending reminder):
1. **AGENTS.md step 5** (opportunistic): main session checks handoff file on every user interaction.
2. **heartbeat Check 1** (hourly safety net, `docs/heartbeat-checks.md`): heartbeat reads handoff file every 60 min, delivers stranded reminders.

Both paths validate before sending: must be JSON with `reminders` array, each entry has string `page_id`, non-empty string `title`, `status` exactly `sent` or `missed`. Malformed → leave file, resolve `OPS_ALERT_SIGNAL_NUMBER` from `.env` to concrete Signal recipient, send ops alert via OpenClaw `message` tool (`action: send`, `channel: signal`, `target: "<resolved OPS_ALERT_SIGNAL_NUMBER>"`) describing the malformed handoff, no delivery, no `complete-reminder` call, no delete. Valid → send each via OpenClaw `message` tool (`action: send`, `channel: signal`), then append/update `state.json.recent_outbound` with a short-lived reminder entry (`type: "reminder"`, `page_id`, `title`, `status`, `sent_at`, `awaiting_response: true`, `expires_at` about 24h later), pruning expired entries, then call `scripts/notion-cli.sh complete-reminder PAGE_ID sent|missed` to atomically set Notion `Status → Completed`, `Reminder Status → sent|missed`, `Completed At`, then delete the handoff file.

Deliberate design: old arch used `sessionTarget: main`, loaded full Opus context (~200k tokens) for a job that 95% finds nothing. Isolated cheap-tier cron cuts cost by orders of magnitude. Trade-off: idle worst-case latency ~75 min (15 min cron + 60 min heartbeat). In practice most reminders still deliver on next user interaction. Practical difference small — cron only fires when REPL idle anyway.

Handoff file = durability boundary. OpenClaw lacks post-announce delivery ack hook, so announce-only flow can't safely call `complete-reminder` before delivery without risking loss on crash. Job stays query-only until hook exists.

## Prompt

```
Run scripts/check-reminders.sh.
Reply with ONLY: NO_REPLY
```

## Notes

- Heartbeat (`docs/heartbeat-checks.md` Checks 2/2b) re-registers if missing, patches if drifted — guards against manual deletion, gateway data loss, or other failure modes that drop the canonical job.
- 15-min polling = recommended cost/latency balance. Affects discovery only; idle delivery still depends on heartbeat or next interaction.
- Cron fires only when REPL idle. Mid-conversation → script waits. Better for ADHD — no mid-task interrupts.
- `check-reminders.sh` queries Notion, writes `.reminder-signal`. Delivery not this job's responsibility.