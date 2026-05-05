# Cron Job: reminder-check

Runs every 30 minutes via OpenClaw's durable cron.

## Registration

```
CronCreate:
  schedule: "*/30 * * * *"
  durable: true
  name: "reminder-check"
  sessionTarget: isolated
  model: litellm/qwen2.5  # must match setup/model-tiers.json cheap
  payload:
    kind: agentTurn
    lightContext: true  # empty bootstrap — cron prompt is self-contained
  timeout-seconds: 300
```

Isolated cheap-tier session (see `setup/model-tiers.json`). Query-only: runs check script, writes handoff file (default: `.reminder-signal`, overridable via `REMINDER_SIGNAL_FILE` in `.env`) if reminders due, exits. Does not deliver.

Most runs only need a Notion query and a possible handoff-file write. Current OpenClaw durable cron registration uses `agentTurn`; the reduced cadence limits routine LLM use.

**Role: backstop.** Primary reminder delivery is the per-reminder one-shot cron registered at intake (`setup/cron/reminder-delivery.md`). This `reminder-check` polling job catches anything that primary path misses: `CronCreate` failures at intake, jobs that fail to fire (gateway down at the scheduled time, etc.), or reminders that lack a registered one-shot for any other reason.

**Backstop delivery paths** (only fire when this poll finds a still-Pending reminder):
1. **AGENTS.md step 6** (opportunistic): main session checks handoff file on every user interaction.
2. **reminder-delivery-sweep** (2-hour idle safety net, `setup/cron/reminder-delivery-sweep.md`): sweep reads the handoff file and delivers stranded reminders.
3. **heartbeat cron Check 1** (daily safety net, `docs/heartbeat-checks.md`): heartbeat reads the handoff file daily, delivers stranded reminders if the narrower sweep did not.

Both paths validate before sending: must be JSON with `reminders` array, each entry has string `page_id`, non-empty string `title`, and string `status`. New handoff writers emit only `sent`; legacy `missed` entries should still be delivered and normalized to `sent`. Any other shape/status is malformed → leave file, resolve `OPS_ALERT_SIGNAL_NUMBER` from `.env` to concrete Signal recipient, send ops alert via OpenClaw `message` tool (`action: send`, `channel: signal`, `target: "<resolved OPS_ALERT_SIGNAL_NUMBER>"`) describing the malformed handoff, no delivery, no `complete-reminder` call, no delete. Valid → send each via OpenClaw `message` tool (`action: send`, `channel: signal`) with uniform shame-safe wording (`Hey, time to [task]`), then append/update `state.json.recent_outbound` with a short-lived reminder entry (`type: "reminder"`, `page_id`, `title`, `status: "sent"`, `sent_at`, `awaiting_response: true`, `expires_at` about 24h later), pruning expired entries, then call `scripts/notion-cli.sh complete-reminder PAGE_ID sent` to atomically set Notion `Status → Completed`, `Reminder Status → sent`, `Completed At`, then delete the handoff file.

Deliberate design: `sessionTarget: isolated` with cheap-tier model keeps per-run cost low. A `sessionTarget: main` full Opus context (~200k tokens) for a job that usually finds nothing would be orders of magnitude more expensive. Trade-off: fully idle safety-net delivery can wait until the narrower `reminder-delivery-sweep` or daily heartbeat. In practice most reminders still deliver through the primary one-shot cron or on next user interaction.

Handoff file = durability boundary. OpenClaw lacks post-announce delivery ack hook, so announce-only flow can't safely call `complete-reminder` before delivery without risking loss on crash. Job stays query-only until hook exists.

## Prompt

```
Run scripts/check-reminders.sh.
Reply with ONLY: NO_REPLY
```

## Notes

- AGENTS.md startup and the `heartbeat` cron (`docs/heartbeat-checks.md` Check 2) re-register if missing. Weekly `janitor` (`docs/heartbeat-checks.md` Check 2b) patches drift — guards against manual deletion, gateway data loss, or other failure modes that drop or stale the canonical job.
- 30-min polling = recommended cost/latency balance. Affects discovery only; idle delivery still depends on `reminder-delivery-sweep`, heartbeat, or next interaction.
- Cron fires only when REPL idle. Mid-conversation → script waits. Better for ADHD — no mid-task interrupts.
- `check-reminders.sh` queries Notion, writes `.reminder-signal`. Delivery not this job's responsibility.
